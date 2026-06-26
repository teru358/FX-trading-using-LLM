from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


def _insert(orch, *, plan_id, lease_offset_sec):
    now = db_now()
    orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=now + timedelta(seconds=lease_offset_sec),
    )


def test_picks_stale_pending(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=1, lease_offset_sec=-60)  # lease 超過 pending
    rows = orch.get_stale_or_unconfirmed_intents(now=db_now())
    assert [r.plan_id for r in rows] == [1]


def test_picks_submitted_without_order_id(tmp_path: Path):
    """送信直後クラッシュ (status=submitted, order_id null) を拾う (codex #1 回帰)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=2, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=2, submitted_at=now)  # status→submitted, order_id まだ null
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert [r.plan_id for r in rows] == [2]


def test_picks_submitted_with_order_id_for_correction(tmp_path: Path):
    """order_id 付き submitted (status 補正前) も recovery 対象 (job が filled 補正、3 分岐目)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=3, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=3, submitted_at=now)
    orch.record_order_result(plan_id=3, status="submitted", order_id="mt5:111")
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert [r.plan_id for r in rows] == [3]


def test_skips_terminal_filled(tmp_path: Path):
    """既に filled (terminal) は recovery 対象外。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=6, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=6, submitted_at=now)
    orch.record_order_result(plan_id=6, status="filled", order_id="mt5:222")
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert rows == []


def test_skips_non_expired_lease(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=4, lease_offset_sec=+300)  # lease まだ有効
    rows = orch.get_stale_or_unconfirmed_intents(now=db_now())
    assert rows == []


def test_set_recovery_status(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=5, lease_offset_sec=-60)
    orch.set_recovery_status(plan_id=5, recovery_status="needs_reconcile")
    intent = orch.get_order_intent(plan_id=5)
    assert intent.recovery_status == "needs_reconcile"
