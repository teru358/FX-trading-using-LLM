"""reflections テーブルと planning 照会 API のテスト (spec §3.2b/§3.4/§3.6)。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.data.orchestrator_store import OrchestratorStore
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

    def test_get_reflections_lists_all(self, store):
        store.mark_reflection_retry("a", pair="P", error="e", now=NOW)
        store.mark_reflection_dead("b", pair="P", error="e", now=NOW)
        ids = {r.order_id for r in store.get_reflections()}
        assert ids == {"a", "b"}


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
        n = store.finish_dangling_runs(now=NOW)
        assert n == 1
        run = store.get_run(rid)
        assert run.status == "failed"
        assert run.error_type == "dangling"
        assert run.finished_at is not None
