"""reflection job のテスト (spec §3.2/§3.2b/§3.3/§3.5/§3.6)。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.cycles.reflection import (
    _dedupe_raw_rows,
    _select_targets,
    run_reflection_cycle,
)
from src.data.orchestrator_store import OrchestratorStore
from src.trading.position_manager import Order
from src.utils.clock import db_now


NOW = db_now()


def _closed_order(oid, pair="USDJPY=X", closed_at=None):
    return Order(
        order_id=oid, pair=pair, direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1000,
        status="closed", closed_at=closed_at or NOW,
        close_price=151.0, close_reason="take_profit", realized_pnl=100.0,
        signal_reason="test",
    )


@pytest.fixture
def orch(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


class TestSelectTargets:
    def test_untried_newest_first_quota_2(self, orch):
        orders = [_closed_order(f"o{i}", closed_at=NOW + timedelta(hours=i))
                  for i in range(12)]
        targets = _select_targets(orders, orch, NOW)
        # 枠 10: 未試行の新しい順 2 (o11, o10) + 残り古い順 8 (o0..o7)
        ids = [t.order_id for t in targets]
        assert ids[:2] == ["o11", "o10"]
        assert ids[2:] == [f"o{i}" for i in range(8)]

    def test_done_and_dead_excluded(self, orch):
        orch.mark_reflection_done("a", plan_id=None, pair="P", close_reason="c",
                                  realized_pnl=0.0, reflection_text="t",
                                  was_directionally_correct=True, now=NOW)
        orch.mark_reflection_dead("b", pair="P", error="e", now=NOW)
        orders = [_closed_order("a"), _closed_order("b"), _closed_order("c")]
        ids = [t.order_id for t in _select_targets(orders, orch, NOW)]
        assert ids == ["c"]

    def test_retry_not_due_excluded_due_included(self, orch):
        orch.mark_reflection_retry("r1", pair="P", error="e", now=NOW)  # +1h
        orders = [_closed_order("r1")]
        assert _select_targets(orders, orch, NOW) == []
        later = NOW + timedelta(hours=1, minutes=1)
        ids = [t.order_id for t in _select_targets(orders, orch, later)]
        assert ids == ["r1"]

    def test_quota_spillover_when_few_untried(self, orch):
        # 未試行 1 件だけなら backfill に枠を融通し合計 10 まで
        orch.mark_reflection_retry("r1", pair="P", error="e", now=NOW - timedelta(hours=2))
        orders = [_closed_order("r1"), _closed_order("new1")]
        ids = {t.order_id for t in _select_targets(orders, orch, NOW)}
        assert ids == {"r1", "new1"}

    def test_untried_exceeding_total_quota(self, orch):
        """untried が枠 10 を超える場合 (レビュー HIGH-2/3)。

        全件が同じ untried プールから来るため fresh/backfill がきれいに
        分割されず、「新しい順 2 + 古い順 8」を単一プールに適用した結果に
        なることを固定する (12 件ケースでは複数の誤実装と区別できない)。
        """
        orders = [_closed_order(f"u{i}", closed_at=NOW + timedelta(hours=i))
                  for i in range(15)]
        ids = [t.order_id for t in _select_targets(orders, orch, NOW)]
        assert ids == ["u14", "u13"] + [f"u{i}" for i in range(8)]

    def test_no_untried_fills_full_quota_from_backfill(self, orch):
        """untried=0 でも retry 到来分だけで枠 10 を埋める。"""
        for i in range(12):
            orch.mark_reflection_retry(f"r{i}", pair="P", error="e",
                                       now=NOW - timedelta(hours=4))
        orders = [_closed_order(f"r{i}", closed_at=NOW + timedelta(hours=i))
                  for i in range(12)]
        ids = [t.order_id for t in _select_targets(orders, orch, NOW)]
        assert ids == [f"r{i}" for i in range(10)]   # 全て古い順 backfill

    def test_closed_at_none_falls_back_to_opened_at(self, orch):
        """closed_at 未設定 (Optional) でも opened_at で順序付けされる。

        Order.closed_at は status=="closed" でも設定保証がないため実データから
        到達可能。_ts の `or opened_at` フォールバックの分岐を固定する。
        """
        old = _closed_order("no-close-old")
        old.closed_at = None                        # helper の既定を明示的に解除
        old.opened_at = NOW - timedelta(hours=10)
        new = _closed_order("no-close-new")
        new.closed_at = None
        new.opened_at = NOW + timedelta(hours=10)
        mid = _closed_order("has-close", closed_at=NOW)
        ids = [t.order_id for t in _select_targets([old, new, mid], orch, NOW)]
        # fresh 2 = opened_at 由来含む新しい順、残りは古い順
        assert ids[:2] == ["no-close-new", "has-close"]
        assert ids[2:] == ["no-close-old"]

    def test_equal_timestamps_order_is_stable(self, orch):
        """同時刻 closed_at では入力順が保たれる (sorted は安定ソート)。"""
        orders = [_closed_order(f"s{i}", closed_at=NOW) for i in range(4)]
        ids = [t.order_id for t in _select_targets(orders, orch, NOW)]
        assert ids[:2] == ["s0", "s1"]          # 新しい順も入力順を維持
        assert set(ids) == {"s0", "s1", "s2", "s3"}

    def test_dead_is_terminal_even_when_retry_due(self, orch):
        """dead は next_retry_at に関係なく再選択されない (terminal)。"""
        orch.mark_reflection_dead("d1", pair="P", error="e", now=NOW)
        orders = [_closed_order("d1")]
        far_future = NOW + timedelta(days=365)
        assert _select_targets(orders, orch, far_future) == []


class TestDedupeRawRows:
    """重複 order_id の排除は parse 前 (raw 段階) が正本 (レビュー MEDIUM)。

    以前は _select_targets が parse 済み Order を潰していたが、後着行が
    壊れていると候補が先着 1 件になり古い決済情報で done 確定してしまう。
    """

    def test_duplicate_order_ids_deduplicated(self):
        """同 order_id が複数あっても候補は 1 件 (LLM/RAG の二重支出防止)。"""
        rows = [{"order_id": "d1", "n": 1}, {"order_id": "d1", "n": 2},
                {"order_id": "other", "n": 3}]
        kept = _dedupe_raw_rows(rows)
        assert [r["order_id"] for r in kept] == ["d1", "other"]

    def test_keeps_last_occurrence(self):
        """closed_at が無い/同値なら append-only の正本は最終出現行。"""
        rows = [{"order_id": "d1", "realized_pnl": 100.0},
                {"order_id": "d1", "realized_pnl": -200.0}]
        kept = _dedupe_raw_rows(rows)
        assert len(kept) == 1
        assert kept[0]["realized_pnl"] == -200.0

    def test_duplicate_prefers_newer_closed_at_regardless_of_order(self):
        """追記順が逆でも closed_at が新しい行を採る (レビュー HIGH-1)。

        append 順 = bot が決済に気づいた時刻、closed_at = 実際の約定時刻で
        両者は decoupled。サーバー側 SL/TP が bot 停止中に発火し、locally
        observed close の後に reconcile されると後着の closed_at が先着より
        古くなる (mt5_bridge_broker.py:632-647 が deal history の closed_at を
        position_manager.py:474 経由で書く)。最終出現規則だけだと、この経路で
        古い約定情報を採ってしまう。
        """
        newer = {"order_id": "d1", "realized_pnl": 999.0,
                 "closed_at": (NOW + timedelta(hours=5)).isoformat()}
        older = {"order_id": "d1", "realized_pnl": 1.0,
                 "closed_at": NOW.isoformat()}
        kept = _dedupe_raw_rows([newer, older])      # 追記順は newer → older
        assert len(kept) == 1
        assert kept[0]["realized_pnl"] == 999.0

    def test_newer_closed_at_wins_when_appended_later(self):
        """通常順 (後着が新しい約定) でも当然新しい方を採る。"""
        older = {"order_id": "d1", "realized_pnl": 1.0,
                 "closed_at": NOW.isoformat()}
        newer = {"order_id": "d1", "realized_pnl": 999.0,
                 "closed_at": (NOW + timedelta(hours=5)).isoformat()}
        kept = _dedupe_raw_rows([older, newer])
        assert kept[0]["realized_pnl"] == 999.0

    def test_unparseable_closed_at_falls_back_to_file_order(self):
        """closed_at が parse 不能なら比較せずファイル順 (最終出現) に倒す。"""
        rows = [{"order_id": "d1", "realized_pnl": 1.0,
                 "closed_at": "not-a-datetime"},
                {"order_id": "d1", "realized_pnl": 999.0,
                 "closed_at": "also-broken"}]
        kept = _dedupe_raw_rows(rows)
        assert len(kept) == 1
        assert kept[0]["realized_pnl"] == 999.0

    def test_missing_closed_at_falls_back_to_file_order(self):
        """片方に closed_at が無いだけでも比較を諦めて最終出現行を採る。"""
        rows = [{"order_id": "d1", "realized_pnl": 1.0,
                 "closed_at": (NOW + timedelta(hours=5)).isoformat()},
                {"order_id": "d1", "realized_pnl": 999.0}]
        kept = _dedupe_raw_rows(rows)
        assert len(kept) == 1
        assert kept[0]["realized_pnl"] == 999.0

    def test_unhashable_order_id_does_not_break_other_rows(self):
        """hashable でない order_id でファイル全体を落とさない (レビュー HIGH-2)。

        手動編集で到達可能。dedupe は行ごとの try の外にあるため、例外が出ると
        _parse_closed_trades を貫通して controller まで届き、全 pending
        reflection が停止する。
        """
        rows = [{"order_id": ["x"]}, {"order_id": "ok"}]
        kept = _dedupe_raw_rows(rows)            # 例外を出さない
        assert {r["order_id"] if isinstance(r["order_id"], str) else "?"
                for r in kept} == {"?", "ok"}

    def test_preserves_file_order_of_kept_rows(self):
        """採用行は trades.json 内の出現順を保つ (選択側の安定ソート前提)。"""
        rows = [{"order_id": "a"}, {"order_id": "b"},
                {"order_id": "a", "later": True}, {"order_id": "c"}]
        kept = _dedupe_raw_rows(rows)
        # a は最終出現位置 (index 2) で残るため b の後ろに来る
        assert [r["order_id"] for r in kept] == ["b", "a", "c"]
        assert kept[1]["later"] is True

    def test_rows_without_order_id_pass_through(self):
        """order_id 無し行は潰しようがないので通す (parse 側で弾く)。"""
        rows = [{"pair": "USDJPY=X"}, {"order_id": "a"}, "not-a-dict"]
        kept = _dedupe_raw_rows(rows)
        assert len(kept) == 3


class _FakeSlot:
    def __init__(self, busy_after=None, waiting=False):
        self.calls = 0
        self._busy_after = busy_after
        self.waiting_user_job = waiting

    def try_run_scheduled(self, fn, *args, **kwargs):
        if self._busy_after is not None and self.calls >= self._busy_after:
            return False
        self.calls += 1
        fn(*args, **kwargs)
        return True


def _config(tmp_path):
    """テスト用の最小 config another。実装の consumer 面に合わせ SimpleNamespace で足りる。"""
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        tradeable_instruments=[SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY")],
        llm=SimpleNamespace(reflection=SimpleNamespace(temperature=0.3)),
        orchestrator=SimpleNamespace(policy=SimpleNamespace(trade_horizon="swing")),
        user_notes_path=tmp_path / "notes.md",
    )


def _write_trades(tmp_path, orders):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "trades.json").write_text(
        json.dumps([o.to_dict() for o in orders]), encoding="utf-8")


def test_config_attribute_paths_resolve_on_real_appconfig():
    """reflection.py が読む config 属性が実 AppConfig で解決すること。

    controller テストの SimpleNamespace fixture は make_embed_fn を monkeypatch で
    潰すため schema drift を検出できない (レビュー MEDIUM-1)。リネームがあれば
    このテストだけが落ちる。
    """
    from src.config.schema import AppConfig
    from src.rag.embedder import make_embed_fn

    config = AppConfig()
    assert config.llm.reflection.temperature is not None
    assert config.orchestrator.policy.trade_horizon is not None
    assert config.state_dir is not None
    assert config.user_notes_path is not None
    assert isinstance(config.tradeable_instruments, list)
    make_embed_fn(config)       # config.rag.* を読む — 欠落なら AttributeError


class TestRunReflectionCycle:
    def test_processes_and_marks_done(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        processed = []

        async def fake_reflect_and_record(config, store, orch_store, llm,
                                          embed_fn, order, entry_analysis):
            processed.append(order.order_id)
            return ("text", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record",
                            fake_reflect_and_record)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert processed == ["o1"]
        assert orch.get_reflection("o1").status == "done"

    def test_unknown_pair_dead_without_llm(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("ox", pair="GONE=X")])
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("ox").status == "dead"

    def test_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])

        async def boom(*args, **kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", boom)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        r = orch.get_reflection("o1")
        assert r.status == "retry"
        assert r.attempt_count == 1

    def test_slot_busy_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1"), _closed_order("o2")])
        slot = _FakeSlot(busy_after=1)
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert len(seen) == 1   # 2 件目で busy → controller 終了

    def test_waiting_user_job_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        slot = _FakeSlot(waiting=True)
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert slot.calls == 0

    def test_running_planning_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        orch.start_run("OrchestratorRuntime", pair="USDJPY=X",
                       trigger_type="planning_cycle")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        slot = _FakeSlot()
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert slot.calls == 0

    def test_trades_json_missing_is_noop(self, tmp_path, orch, monkeypatch):
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())   # 例外なく終了

    def test_broken_row_does_not_block_valid_rows(self, tmp_path, orch, monkeypatch):
        """不正行混在でも正常行は処理される (plan レビュー Medium-2)。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        good = _closed_order("good-1").to_dict()
        broken = {"order_id": "bad-1", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}   # from_dict が落ちる行
        no_id = {"pair": "USDJPY=X"}               # order_id なし不正行
        (state / "trades.json").write_text(
            json.dumps([broken, no_id, good]), encoding="utf-8")
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["good-1"]                        # 正常行は処理
        r = orch.get_reflection("bad-1")
        assert r.status == "retry"                       # 壊れ行は retry (即 dead にしない)
        assert "parse_error" in r.last_error

    def test_parse_retry_recovers_after_fix(self, tmp_path, orch, monkeypatch):
        """parser/データ修正後、retry 行は backoff 到来で正常処理される。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        # 1 回目: 壊れ行 → retry
        broken = {"order_id": "fix-1", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}
        (state / "trades.json").write_text(json.dumps([broken]), encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("fix-1").status == "retry"
        # 2 回目: データ修正 + backoff 経過相当 → 処理されて done
        (state / "trades.json").write_text(
            json.dumps([_closed_order("fix-1").to_dict()]), encoding="utf-8")
        with orch._engine.connect() as conn:
            import sqlalchemy as sa
            conn.execute(sa.text(
                "UPDATE reflections SET next_retry_at = '2000-01-01 00:00:00' "
                "WHERE order_id='fix-1'"))
            conn.commit()
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["fix-1"]
        assert orch.get_reflection("fix-1").status == "done"

    def test_parse_error_respects_backoff_deadline(self, tmp_path, orch,
                                                    monkeypatch):
        """backoff 期限前の再走査で attempt を増やさない (レビュー MEDIUM-1)。

        毎回無条件に retry を記録すると、毎時実行では 1/2/4/8h の backoff を
        待たずに約 5 時間で dead に落ちる (本来は 15 時間かかる設計)。
        """
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        broken = {"order_id": "bk-1", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}
        (state / "trades.json").write_text(json.dumps([broken]), encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        for _ in range(5):      # backoff 中に何度走査しても 1 回のまま
            run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                                 slot=_FakeSlot())
        r = orch.get_reflection("bk-1")
        assert r.status == "retry"
        assert r.attempt_count == 1
        assert r.next_retry_at == r.created_at + timedelta(hours=1)

    def test_parse_error_retries_after_deadline(self, tmp_path, orch,
                                                monkeypatch):
        """期限到来後の走査では attempt が進む。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        broken = {"order_id": "bk-2", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}
        (state / "trades.json").write_text(json.dumps([broken]), encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("bk-2").attempt_count == 1
        import sqlalchemy as sa
        with orch._engine.connect() as conn:
            conn.execute(sa.text(
                "UPDATE reflections SET next_retry_at = '2000-01-01 00:00:00' "
                "WHERE order_id='bk-2'"))
            conn.commit()
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("bk-2").attempt_count == 2

    def test_parse_error_skipped_for_terminal_rows(self, tmp_path, orch,
                                                   monkeypatch):
        """done / dead 行は parse 対象外 (retry 記録もしない)。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        rows = [{"order_id": "done-1", "pair": "USDJPY=X",
                 "opened_at": "not-a-datetime"},
                {"order_id": "dead-1", "pair": "USDJPY=X",
                 "opened_at": "not-a-datetime"}]
        (state / "trades.json").write_text(json.dumps(rows), encoding="utf-8")
        orch.mark_reflection_done("done-1", plan_id=None, pair="USDJPY=X",
                                  close_reason="c", realized_pnl=0.0,
                                  reflection_text="t",
                                  was_directionally_correct=True, now=NOW)
        orch.mark_reflection_dead("dead-1", pair="USDJPY=X", error="e", now=NOW)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("done-1").status == "done"
        assert orch.get_reflection("done-1").attempt_count == 0
        assert orch.get_reflection("dead-1").attempt_count == 0

    def test_broken_later_duplicate_does_not_confirm_earlier_row(
            self, tmp_path, orch, monkeypatch):
        """後着が壊れているとき、先着の古い決済情報で done にしない (レビュー MEDIUM)。

        append-only の正本は最終出現行。後着 = 補正行が parse できない場合に
        先着へフォールバックすると、古い PnL で振り返って done 確定し、
        補正情報が永久に失われる。候補を raw 段階で 1 件へ絞ることで、
        壊れた後着は retry に留まり先着は採用されない。
        """
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        good = _closed_order("dup-1").to_dict()          # 先着 = 正常 (PnL +100)
        broken = {"order_id": "dup-1", "pair": "USDJPY=X",
                  "realized_pnl": -200.0,
                  "opened_at": "not-a-datetime"}         # 後着 = 壊れた補正行
        (state / "trades.json").write_text(
            json.dumps([good, broken]), encoding="utf-8")
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == []                        # 先着では振り返らない
        r = orch.get_reflection("dup-1")
        assert r.status == "retry"               # 後着の parse error が残る
        assert "parse_error" in r.last_error

    def test_broken_earlier_duplicate_yields_to_valid_later_row(
            self, tmp_path, orch, monkeypatch):
        """先着が壊れていても、後着が正常なら後着で処理される。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        broken = {"order_id": "dup-2", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}
        good = _closed_order("dup-2").to_dict()
        (state / "trades.json").write_text(
            json.dumps([broken, good]), encoding="utf-8")
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["dup-2"]                 # 壊れた先着は無視される
        assert orch.get_reflection("dup-2").status == "done"

    def test_broken_duplicates_advance_attempt_only_once_per_cycle(
            self, tmp_path, orch, monkeypatch):
        """壊れた重複行 5 件でも 1 サイクルで attempt は 1 しか進まない。

        parse ループが行ごとに retry を記録すると、1 回の走査で backoff を
        飛び越して dead に落ちる (5 件で即 dead)。
        """
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        rows = [{"order_id": "dup-3", "pair": "USDJPY=X",
                 "opened_at": "not-a-datetime", "seq": i} for i in range(5)]
        (state / "trades.json").write_text(json.dumps(rows), encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        r = orch.get_reflection("dup-3")
        assert r.status == "retry"
        assert r.attempt_count == 1

    def test_valid_duplicates_use_latest_row_end_to_end(
            self, tmp_path, orch, monkeypatch):
        """正常な重複行同士では後着が採用される (既存挙動の維持)。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        old = _closed_order("dup-4", closed_at=NOW)
        old.realized_pnl = 100.0
        new = _closed_order("dup-4", closed_at=NOW + timedelta(hours=1))
        new.realized_pnl = -200.0
        (state / "trades.json").write_text(
            json.dumps([old.to_dict(), new.to_dict()]), encoding="utf-8")
        got = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            got.append(order.realized_pnl)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert got == [-200.0]                   # 後着の補正 PnL で振り返る

    def test_unhashable_order_id_does_not_halt_cycle(self, tmp_path, orch,
                                                     monkeypatch):
        """不正な order_id 1 行で全 pending reflection を止めない (レビュー HIGH-2)。

        修正前は Order.from_dict の行ごと try に封じ込められていたが、dedupe を
        parse 前に出したことで try の外に露出した。controller の try は
        load_trades_raw しか守っていないため、例外は呼び出し元まで貫通する。
        """
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        rows = [{"order_id": ["x"], "pair": "USDJPY=X"},
                _closed_order("good-1").to_dict()]
        (state / "trades.json").write_text(json.dumps(rows), encoding="utf-8")
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["good-1"]                # 正常行は処理される
        assert orch.get_reflection("good-1").status == "done"

    def test_reconciled_duplicate_uses_actual_fill_time(self, tmp_path, orch,
                                                        monkeypatch):
        """後着が古い約定なら、先着 (新しい約定) の PnL で振り返る (HIGH-1)。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        # 先着 = locally observed close (新しい約定時刻)
        local = _closed_order("dup-5", closed_at=NOW + timedelta(hours=5))
        local.realized_pnl = 999.0
        # 後着 = 後から reconcile された古いサーバー側 SL/TP 決済
        reconciled = _closed_order("dup-5", closed_at=NOW)
        reconciled.realized_pnl = 1.0
        (state / "trades.json").write_text(
            json.dumps([local.to_dict(), reconciled.to_dict()]),
            encoding="utf-8")
        got = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            got.append(order.realized_pnl)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert got == [999.0]

    def test_trades_json_not_a_list_is_noop(self, tmp_path, orch, monkeypatch):
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "trades.json").write_text('{"oops": true}', encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())   # warning のみで終了

    def test_plan_context_passed(self, tmp_path, orch, monkeypatch):
        """order_intent → plan_id → reasoning が解決される。

        plan_id は reflection prompt には渡らない (plan 文脈は entry_analysis 経由
        のみ、レビュー MEDIUM-2)。到達先は reflections 行の plan_id 列。
        """
        _write_trades(tmp_path, [_closed_order("bro-1")])
        # order_intent を仕込む (order_id="bro-1", plan_id=42)。
        # 現行シグネチャ: owner_run_id: int + lease_until: datetime 必須
        import sqlalchemy as sa
        owner = orch.start_run("OrchestratorRuntime", pair="USDJPY=X",
                               trigger_type="watch_cycle")
        orch.try_insert_order_intent(
            plan_id=42, pair="USDJPY=X", intended_action="buy",
            owner_run_id=owner, lease_until=NOW + timedelta(seconds=60),
            trigger_id="t", decision_id=None)
        with orch._engine.connect() as conn:
            conn.execute(sa.text(
                "UPDATE order_intents SET order_id='bro-1' WHERE plan_id=42"))
            conn.commit()
        # reasoning getter は record_decision の仕込みが重いので instance mock で固定
        monkeypatch.setattr(orch, "get_latest_plan_create_reasoning",
                            lambda pid: f"reasoning-for-{pid}")
        got = {}

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            got["entry_analysis"] = entry_analysis
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert got["entry_analysis"] == "reasoning-for-42"   # 実文字列が渡る
        assert orch.get_reflection("bro-1").plan_id == 42    # 記録先は reflections 行

    def test_context_lookup_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        """plan 文脈取得の失敗も 1 件単位の例外境界に入る (レビュー Medium-4)。"""
        _write_trades(tmp_path, [_closed_order("o1")])
        def boom(order_id):
            raise RuntimeError("db locked")
        monkeypatch.setattr(orch, "get_order_intent_by_order_id", boom)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        # controller は例外を漏らさず終了し、retry 行が残る
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        r = orch.get_reflection("o1")
        assert r.status == "retry"

    def test_dead_save_failure_does_not_abort_batch(self, tmp_path, orch,
                                                    monkeypatch):
        """dead 記録の失敗が batch の残りを巻き添えにしない (レビュー HIGH-1)。

        PriorityJobSlot.try_run_scheduled は fn の例外を握らず再送出するため、
        dead-lettering が try の外にあると 1 件目の DB 失敗で残り最大 9 件が
        黙って落ちる。
        """
        _write_trades(tmp_path, [_closed_order("ox", pair="GONE=X"),
                                 _closed_order("o2")])
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        def boom(*args, **kwargs):
            raise RuntimeError("db locked")

        monkeypatch.setattr(orch, "mark_reflection_dead", boom)
        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        # 例外は controller へ漏れない
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["o2"]                        # 2 件目の正常 order は処理される
        assert orch.get_reflection("o2").status == "done"

    def test_resumes_untouched_targets_after_early_stop(self, tmp_path, orch,
                                                        monkeypatch):
        """controller 途中終了後の再開で取りこぼしも重複処理も起きない。

        設計の中核主張 (spec §3.6): slot busy で中断した回の未着手 target には
        何も書かれず、次回実行でそのまま処理される。
        """
        orders = [_closed_order("o1", closed_at=NOW),
                  _closed_order("o2", closed_at=NOW + timedelta(hours=1)),
                  _closed_order("o3", closed_at=NOW + timedelta(hours=2))]
        _write_trades(tmp_path, orders)
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        # 1 回目: 1 件処理した時点で slot busy → 残りは未着手のまま
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot(busy_after=1))
        assert len(seen) == 1
        first = seen[0]
        for oid in ("o1", "o2", "o3"):
            r = orch.get_reflection(oid)
            if oid == first:
                assert r.status == "done"
            else:
                assert r is None            # 未着手 target には何も書かれない
        # 2 回目: 残り 2 件が処理され、済んだ 1 件は再処理されない
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert sorted(seen) == ["o1", "o2", "o3"]           # 取りこぼしなし
        assert len(seen) == len(set(seen))                  # 重複処理なし

    def test_done_save_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        """RAG 成功後の done 保存失敗 → retry (次回 RAG upsert は冪等)。"""
        _write_trades(tmp_path, [_closed_order("o1")])

        async def ok(config, store, orch_store, llm, embed_fn, order,
                     entry_analysis):
            return ("t", True)

        calls = {"n": 0}
        real_done = orch.mark_reflection_done
        def flaky_done(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("db locked")
        monkeypatch.setattr(orch, "mark_reflection_done", flaky_done)
        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", ok)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert calls["n"] == 1
        r = orch.get_reflection("o1")
        assert r.status == "retry"      # 処理済みにはならない → 次回再処理
