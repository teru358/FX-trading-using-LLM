"""reflections テーブルと planning 照会 API のテスト (spec §3.2b/§3.4/§3.6)。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from src.data.orchestrator_store import (
    _PLANNING_STALE_SECONDS,
    _REFLECTION_MAX_ATTEMPTS,
    OrchestratorStore,
)
from src.utils.clock import db_now


@pytest.fixture
def store(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


NOW = db_now()


class TestReflectionCrud:
    def test_get_reflection_missing_returns_none(self, store):
        assert store.get_reflection("no-such-order") is None

    def test_mark_done_upserts_and_get_returns_done(self, store):
        store.mark_reflection_done(
            "ord-1", plan_id=7, pair="USDJPY=X", close_reason="take_profit",
            realized_pnl=120.0, reflection_text="lesson text",
            was_directionally_correct=True, now=NOW,
        )
        r = store.get_reflection("ord-1")
        assert r.status == "done"
        assert r.plan_id == 7
        assert r.was_directionally_correct is True
        assert r.reflection_text == "lesson text"

    def test_mark_done_allows_null_plan_id(self, store):
        store.mark_reflection_done(
            "ord-legacy", plan_id=None, pair="EURUSD=X", close_reason="manual",
            realized_pnl=-5.0, reflection_text="t",
            was_directionally_correct=False, now=NOW,
        )
        assert store.get_reflection("ord-legacy").plan_id is None

    def test_retry_increments_and_sets_backoff(self, store):
        store.mark_reflection_retry("ord-2", pair="USDJPY=X", error="LLM timeout", now=NOW)
        r = store.get_reflection("ord-2")
        assert r.status == "retry"
        assert r.attempt_count == 1
        assert r.last_error == "LLM timeout"
        assert r.next_retry_at == NOW + timedelta(hours=1)   # backoff[0]

    def test_backoff_progression_1_2_4_8(self, store):
        # 5 回目の失敗は dead になるため、backoff は 4 段 (1/2/4/8h) で全段使われる
        expected_hours = [1, 2, 4, 8]
        for i, h in enumerate(expected_hours, start=1):
            store.mark_reflection_retry("ord-3", pair="USDJPY=X", error="e", now=NOW)
            r = store.get_reflection("ord-3")
            assert r.status == "retry"
            assert r.attempt_count == i
            assert r.next_retry_at == NOW + timedelta(hours=h)

    def test_fifth_failure_becomes_dead(self, store):
        for _ in range(5):
            store.mark_reflection_retry("ord-4", pair="USDJPY=X", error="e", now=NOW)
        r = store.get_reflection("ord-4")
        assert r.status == "dead"
        assert r.attempt_count == 5

    def test_mark_dead_directly(self, store):
        store.mark_reflection_dead("ord-5", pair="XXXYYY=X",
                                   error="pair not in instruments", now=NOW)
        r = store.get_reflection("ord-5")
        assert r.status == "dead"
        assert "not in instruments" in r.last_error

    def test_retry_then_done_clears_error_state(self, store):
        store.mark_reflection_retry("ord-6", pair="USDJPY=X", error="e", now=NOW)
        store.mark_reflection_done(
            "ord-6", plan_id=None, pair="USDJPY=X", close_reason="stop_loss",
            realized_pnl=-30.0, reflection_text="t",
            was_directionally_correct=False, now=NOW,
        )
        r = store.get_reflection("ord-6")
        assert r.status == "done"
        assert r.last_error is None        # done 遷移で error 状態を消去
        assert r.next_retry_at is None

    def test_done_preserves_attempt_count(self, store):
        """done は attempt_count をリセットしない。

        「何回失敗した末に成功したか」は reflection job の健全性指標として
        残す (リセットすると retry 経由の成功が観測できなくなる)。
        """
        store.mark_reflection_retry("ord-7", pair="USDJPY=X", error="e", now=NOW)
        store.mark_reflection_done(
            "ord-7", plan_id=None, pair="USDJPY=X", close_reason="take_profit",
            realized_pnl=10.0, reflection_text="t",
            was_directionally_correct=True, now=NOW,
        )
        r = store.get_reflection("ord-7")
        assert r.status == "done"
        assert r.attempt_count == 1

    def test_dead_is_terminal_retry_is_noop(self, store):
        """dead は terminal。後続 retry で復活してはならない (HIGH-3)。"""
        store.mark_reflection_dead("ord-8", pair="USDJPY=X",
                                   error="pair not in instruments", now=NOW)
        store.mark_reflection_retry("ord-8", pair="USDJPY=X", error="later", now=NOW)
        r = store.get_reflection("ord-8")
        assert r.status == "dead"
        assert r.attempt_count == 0          # no-op なので増えない
        assert "not in instruments" in r.last_error   # 元の error を保持

    def test_dead_by_max_attempts_is_terminal(self, store):
        """attempt 上限で dead になった行も同様に terminal。"""
        for _ in range(5):
            store.mark_reflection_retry("ord-9", pair="USDJPY=X", error="e", now=NOW)
        store.mark_reflection_retry("ord-9", pair="USDJPY=X", error="again", now=NOW)
        r = store.get_reflection("ord-9")
        assert r.status == "dead"
        assert r.attempt_count == 5

    def test_mark_done_twice_is_idempotent(self, store):
        for _ in range(2):
            store.mark_reflection_done(
                "ord-10", plan_id=3, pair="USDJPY=X", close_reason="take_profit",
                realized_pnl=50.0, reflection_text="t",
                was_directionally_correct=True, now=NOW,
            )
        assert len(store.get_reflections()) == 1
        r = store.get_reflection("ord-10")
        assert r.status == "done"
        assert r.attempt_count == 0

    def test_done_is_terminal_retry_does_not_revert(self, store):
        """done は terminal。後着 retry で巻き戻ってはならない (HIGH-2)。"""
        store.mark_reflection_done(
            "ord-d1", plan_id=1, pair="P", close_reason="take_profit",
            realized_pnl=1.0, reflection_text="t",
            was_directionally_correct=True, now=NOW)
        store.mark_reflection_retry("ord-d1", pair="P", error="late", now=NOW)
        r = store.get_reflection("ord-d1")
        assert r.status == "done"
        assert r.attempt_count == 0
        assert r.last_error is None
        assert r.reflection_text == "t"

    def test_done_is_terminal_dead_does_not_revert(self, store):
        store.mark_reflection_done(
            "ord-d2", plan_id=1, pair="P", close_reason="take_profit",
            realized_pnl=1.0, reflection_text="t",
            was_directionally_correct=True, now=NOW)
        store.mark_reflection_dead("ord-d2", pair="P", error="late", now=NOW)
        r = store.get_reflection("ord-d2")
        assert r.status == "done"
        assert r.last_error is None

    def test_dead_is_terminal_done_does_not_revert(self, store):
        """dead → done も禁じる (HIGH-2)。dead は恒久不能の記録。"""
        store.mark_reflection_dead("ord-d3", pair="P", error="gone", now=NOW)
        store.mark_reflection_done(
            "ord-d3", plan_id=1, pair="P", close_reason="take_profit",
            realized_pnl=1.0, reflection_text="t",
            was_directionally_correct=True, now=NOW)
        r = store.get_reflection("ord-d3")
        assert r.status == "dead"
        assert "gone" in r.last_error
        assert r.reflection_text is None

    def test_retry_can_transition_to_done_and_dead(self, store):
        """retry からは 3 状態すべてへ遷移できる (回帰ガード)。"""
        store.mark_reflection_retry("ord-t1", pair="P", error="e", now=NOW)
        store.mark_reflection_dead("ord-t1", pair="P", error="gone", now=NOW)
        assert store.get_reflection("ord-t1").status == "dead"
        store.mark_reflection_retry("ord-t2", pair="P", error="e", now=NOW)
        store.mark_reflection_done(
            "ord-t2", plan_id=None, pair="P", close_reason="c",
            realized_pnl=0.0, reflection_text="t",
            was_directionally_correct=True, now=NOW)
        assert store.get_reflection("ord-t2").status == "done"

    def test_retry_rolls_back_entirely_when_terminal_update_fails(
            self, store, monkeypatch):
        """中間 UPDATE 失敗で attempt 増分ごと rollback されること (HIGH-1)。

        commit が 2 回に分かれていると status=retry / next_retry_at=NULL の
        「永久に選ばれない行」が残る。1 トランザクションなら行自体が残らない。
        """
        import src.data.orchestrator_store as mod

        orig = mod.update
        calls = {"n": 0}

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("db exploded")

        monkeypatch.setattr(mod, "update", boom)
        with pytest.raises(RuntimeError):
            store.mark_reflection_retry("ord-rb", pair="P", error="e", now=NOW)
        monkeypatch.setattr(mod, "update", orig)
        assert calls["n"] == 1
        assert store.get_reflection("ord-rb") is None   # 行ごと巻き戻る

    def test_get_reflections_lists_all(self, store):
        store.mark_reflection_retry("a", pair="P", error="e", now=NOW)
        store.mark_reflection_dead("b", pair="P", error="e", now=NOW)
        ids = {r.order_id for r in store.get_reflections()}
        assert ids == {"a", "b"}


class TestReflectionConcurrency:
    """_get_engine は path で engine をキャッシュし全スレッドで共有するため
    (price_store.py:52-61)、orchestrator runtime / scheduler / API スレッドが
    同一 DB を並行に触る。read-modify-write では lost update が起きる。
    """

    def _run_parallel(self, fn, n):
        errors: list[BaseException] = []
        barrier = threading.Barrier(n)

        def worker():
            try:
                barrier.wait()      # 全スレッドを同時に突入させ競合を最大化
                fn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_concurrent_retry_counts_every_attempt(self, store):
        """8 スレッド並行 retry で attempt_count が過小計上されないこと。

        過小計上は dead-letter 到達失敗 = 永久 retry を招き、
        spec §3.2b の 5 回上限という中核不変条件が破綻する。
        """
        # 上限 5 で dead に落ちないよう、上限を超えた回数でも増分が失われないことを見る
        errors = self._run_parallel(
            lambda: store.mark_reflection_retry(
                "ord-race", pair="USDJPY=X", error="e", now=NOW),
            8,
        )
        assert errors == [], f"並行 retry で例外: {errors!r}"
        r = store.get_reflection("ord-race")
        # 最終値はほとんどの試行で 8 になり、dead ガードが発火した試行でのみ
        # 僅かに下回る (実測 40 試行中 38 が 8、残りが 7)。5〜6 が最終値として
        # 出ることはまず無い — dead-letter ログに出る attempt 番号の揺れとは
        # 別物なので混同しないこと。
        # 増分ロストではない: dead 判定を挟まない純粋な増分パスは 8 並行で
        # 必ず 8 になることを test_concurrent_increment_loses_nothing で担保。
        # 守るべき不変条件は「過小計上で dead-letter に到達し損ねないこと」。
        # read-modify-write 版はここで 2 まで落ち、永久 retry を招いていた。
        assert r.attempt_count >= _REFLECTION_MAX_ATTEMPTS
        assert r.status == "dead"

    def test_concurrent_dead_keeps_next_retry_at_null(self, tmp_path):
        """dead 行に next_retry_at が残らないこと (MEDIUM-5)。

        終端 UPDATE に状態条件が無いと、attempt=4 を読んでいた遅れスレッドが
        dead 確定後に next_retry_at を書き戻し、status='dead' かつ
        next_retry_at 非 NULL の不整合が生じる。dead-letter 不変条件自体は
        壊れないが、reflection job が due な retry を
        `WHERE next_retry_at <= now` で拾うと dead 行を拾い続ける。

        低頻度 (実測 ~12%) の競合なので試行を重ねて検出する。
        """
        offenders = []
        for trial in range(40):
            store = OrchestratorStore(tmp_path / f"race-{trial}.db")
            oid = "ord-dead-race"
            errors = self._run_parallel(
                lambda s=store, o=oid: s.mark_reflection_retry(
                    o, pair="USDJPY=X", error="e", now=NOW),
                10,
            )
            assert errors == [], f"並行 retry で例外: {errors!r}"
            r = store.get_reflection(oid)
            assert r.status == "dead"
            if r.next_retry_at is not None:
                offenders.append((trial, r.attempt_count, r.next_retry_at))
        assert offenders == [], (
            f"dead 行に next_retry_at が残存: {len(offenders)}/40 件 {offenders[:3]}"
        )

    def test_concurrent_increment_loses_nothing(self, store):
        """増分パス単体では 1 件も失われないこと (HIGH-1 の本丸)。

        mark_reflection_retry 経由だと terminal ガードの no-op が混ざり
        最終値が interleaving 依存になるため、増分だけを 8 並行で叩いて
        lost update が無いことを厳密に (== 8) 押さえる。

        _upsert_reflection は commit しない契約になった (HIGH-1) ため、
        呼び出し側と同様にここで commit する。
        """
        from sqlalchemy.orm import Session

        def bump():
            with Session(store._engine) as session:
                store._upsert_reflection(
                    session, "ord-inc", pair="USDJPY=X", now=NOW,
                    values={"status": "retry", "last_error": "e"},
                    attempt_increment=True,
                )
                session.commit()

        errors = self._run_parallel(bump, 8)
        assert errors == [], f"並行増分で例外: {errors!r}"
        assert store.get_reflection("ord-inc").attempt_count == 8

    def test_concurrent_first_insert_does_not_raise(self, store):
        """同一 order_id への初回 INSERT 競合で IntegrityError が漏れないこと。"""
        errors = self._run_parallel(
            lambda: store.mark_reflection_dead(
                "ord-insert-race", pair="USDJPY=X", error="e", now=NOW),
            6,
        )
        assert errors == [], f"初回 INSERT 競合で例外: {errors!r}"
        assert len(store.get_reflections()) == 1

    def _race_pair(self, tmp_path, first, second, trials=20):
        """2 種の marker を同時に叩き、監視スレッドで状態遷移列を採る。

        最終スナップショットだけでは「一度 done になった後 retry へ落ちて
        また done に戻る」巻き戻りを検出できないため、レース中の status を
        観測して terminal 状態からの離脱を直接見る。
        """
        observations = []
        for trial in range(trials):
            store = OrchestratorStore(tmp_path / f"pair-{trial}.db")
            oid = "ord-pair-race"
            fns = [lambda s=store, o=oid: first(s, o),
                   lambda s=store, o=oid: second(s, o)] * 4
            errors: list[BaseException] = []
            seen: list[str] = []
            stop = threading.Event()
            barrier = threading.Barrier(len(fns) + 1)

            def worker(fn):
                try:
                    barrier.wait()
                    fn()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def monitor():
                barrier.wait()
                while not stop.is_set():
                    try:
                        r = store.get_reflection(oid)
                    except Exception:       # noqa: BLE001 — SQLite busy は無視
                        continue
                    if r is not None and (not seen or seen[-1] != r.status):
                        seen.append(r.status)

            threads = [threading.Thread(target=worker, args=(f,)) for f in fns]
            mon = threading.Thread(target=monitor, daemon=True)
            mon.start()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            mon.join(timeout=5)
            assert errors == [], f"並行 marker で例外: {errors!r}"
            final = store.get_reflection(oid)
            if not seen or seen[-1] != final.status:
                seen.append(final.status)
            # terminal (done/dead) に一度入ったら、以後 status は変わらない
            for i, st in enumerate(seen):
                if st in ("done", "dead"):
                    assert set(seen[i:]) == {st}, (
                        f"terminal 状態が巻き戻った: {seen}")
                    break
            observations.append(final)
        return observations

    @staticmethod
    def _mark_done(s, o):
        s.mark_reflection_done(
            o, plan_id=1, pair="P", close_reason="take_profit",
            realized_pnl=1.0, reflection_text="t",
            was_directionally_correct=True, now=NOW)

    @staticmethod
    def _mark_retry(s, o):
        s.mark_reflection_retry(o, pair="P", error="e", now=NOW)

    @staticmethod
    def _mark_dead(s, o):
        s.mark_reflection_dead(o, pair="P", error="gone", now=NOW)

    def test_concurrent_done_vs_retry_never_reverts_done(self, tmp_path):
        """done が確定したら以後 retry は上書きできない (HIGH-2 並行版)。"""
        finals = self._race_pair(tmp_path, self._mark_done, self._mark_retry)
        for r in finals:
            if r.status == "done":
                assert r.reflection_text == "t"
                assert r.next_retry_at is None

    def test_concurrent_done_vs_dead_is_stable(self, tmp_path):
        finals = self._race_pair(tmp_path, self._mark_done, self._mark_dead)
        for r in finals:
            assert r.status in ("done", "dead")
            if r.status == "done":
                assert r.reflection_text == "t"
            else:
                assert r.reflection_text is None

    def test_concurrent_dead_vs_done_is_stable(self, tmp_path):
        finals = self._race_pair(tmp_path, self._mark_dead, self._mark_done)
        for r in finals:
            assert r.status in ("done", "dead")

    def test_concurrent_done_does_not_raise(self, store):
        errors = self._run_parallel(
            lambda: store.mark_reflection_done(
                "ord-done-race", plan_id=1, pair="USDJPY=X",
                close_reason="take_profit", realized_pnl=1.0,
                reflection_text="t", was_directionally_correct=True, now=NOW),
            6,
        )
        assert errors == [], f"並行 done で例外: {errors!r}"
        assert len(store.get_reflections()) == 1
        assert store.get_reflection("ord-done-race").status == "done"


class TestOrderIntentLookup:
    def test_get_by_order_id(self, store):
        import sqlalchemy as sa
        owner = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                                trigger_type="watch_cycle")
        ok = store.try_insert_order_intent(
            plan_id=1, pair="USDJPY=X", intended_action="buy",
            owner_run_id=owner, lease_until=NOW + timedelta(seconds=60),
            trigger_id="t1", decision_id=None,
        )
        assert ok
        # order_id は broker 送信後に order worker がセットする運用カラム。
        # 逆引き getter だけが実装対象なので、テストでは生 UPDATE で再現する。
        with store._engine.connect() as conn:
            conn.execute(sa.text(
                "UPDATE order_intents SET order_id='broker-123' WHERE plan_id=1"))
            conn.commit()
        found = store.get_order_intent_by_order_id("broker-123")
        assert found is not None
        assert found.plan_id == 1

    def test_get_by_order_id_missing(self, store):
        assert store.get_order_intent_by_order_id("nope") is None


class TestPlanningRunQuery:
    def test_no_runs_returns_false(self, store):
        assert store.has_running_planning_run(now=NOW) is False

    def test_running_planning_run_returns_true(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="planning_cycle")
        assert store.has_running_planning_run(now=NOW) is True

    def test_finished_run_returns_false(self, store):
        rid = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                              trigger_type="planning_cycle")
        store.finish_run(rid)
        assert store.has_running_planning_run(now=NOW) is False

    def test_stale_run_excluded(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="planning_cycle")
        # start_run は db_now() を内部で打つため、閾値は「実際の started_at」基準の
        # 現在時刻から測る (module-level NOW 基準だとテスト実行の経過秒でずれる)。
        later = db_now() + timedelta(seconds=601)
        assert store.has_running_planning_run(now=later) is False

    def test_other_trigger_types_ignored(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="watch_cycle")
        assert store.has_running_planning_run(now=NOW) is False

    def test_finish_dangling_runs(self, store):
        rid = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                              trigger_type="planning_cycle")
        # stale 閾値を越えた「明らかに古い」run のみが回収対象 (MEDIUM-1)。
        later = db_now() + timedelta(seconds=_PLANNING_STALE_SECONDS + 1)
        n = store.finish_dangling_runs(now=later)
        assert n == 1
        run = store.get_run(rid)
        assert run.status == "failed"
        assert run.error_type == "dangling"
        assert run.finished_at is not None

    def test_finish_dangling_runs_spares_fresh_runs(self, store):
        """多重起動時に、他プロセスの実行中 run を横から failed にしないこと。

        これを踏むと has_running_planning_run が false を返し、
        reflection job の LLM 譲り判定まで壊れる (MEDIUM-1)。
        """
        rid = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                              trigger_type="planning_cycle")
        n = store.finish_dangling_runs(now=db_now())
        assert n == 0
        run = store.get_run(rid)
        assert run.status == "ok"
        assert run.finished_at is None
