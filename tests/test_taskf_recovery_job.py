from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.order_recovery import recover_pending_intents
from src.utils.clock import db_now

NOW = db_now()
FUTURE = NOW + timedelta(days=1)


def _stale_intent_with_plan(orch, *, plan_id):
    """triggered 済 plan + lease 超過 intent を作る (クラッシュ状況を模擬)。"""
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    created = orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=FUTURE, created_by_run_id=run_id,
    )
    assert created == plan_id
    orch.update_plan_status(plan_id, "triggered")
    orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=NOW - timedelta(seconds=60),
    )


def test_pending_becomes_retryable_and_invalidates_plan(tmp_path: Path):
    """未送信クラッシュ: recovery_status=retryable, intent=abandoned, plan=invalidated。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    _stale_intent_with_plan(orch, plan_id=1)
    summary = recover_pending_intents(orch, now=db_now())
    intent = orch.get_order_intent(plan_id=1)
    assert intent.recovery_status == "retryable"
    assert intent.status == "abandoned"             # terminal 化 (新 plan で再発注)
    assert orch.get_trade_plan(1).status == "invalidated"
    assert summary["retryable"] == 1


def test_submitted_without_order_id_becomes_needs_reconcile(tmp_path: Path):
    """送信直後クラッシュ: needs_reconcile, plan は triggered のまま隔離 (terminal 化しない)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent_with_plan(orch, plan_id=1)
    orch.mark_order_submitted(plan_id=1, submitted_at=now)  # order_id まだ null
    summary = recover_pending_intents(orch, now=now)
    intent = orch.get_order_intent(plan_id=1)
    assert intent.recovery_status == "needs_reconcile"
    assert intent.status == "submitted"             # 触らない (建玉あるかもしれない)
    assert orch.get_trade_plan(1).status == "triggered"  # terminal 化しない (隔離)
    assert summary["needs_reconcile"] == 1


def test_submitted_with_order_id_corrected_to_filled(tmp_path: Path):
    """約定確定だが status 補正前: status を filled に補正 (3 分岐目、codex Medium)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent_with_plan(orch, plan_id=1)
    orch.mark_order_submitted(plan_id=1, submitted_at=now)
    orch.record_order_result(plan_id=1, status="submitted", order_id="mt5:1")  # 約定済模擬
    summary = recover_pending_intents(orch, now=now + timedelta(seconds=120))
    assert orch.get_order_intent(plan_id=1).status == "filled"
    assert summary["corrected_filled"] == 1
