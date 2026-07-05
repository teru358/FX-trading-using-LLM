"""trigger 時の spread_pips 記録 (spec S-5): hindsight spread 採点の入力。"""
from datetime import datetime

import pytest

from src.data.orchestrator_store import OrchestratorStore


@pytest.fixture
def store(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


def test_record_shadow_trigger_stores_spread_pips(store):
    trig_id = store.record_shadow_trigger(
        plan_id=1, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=datetime(2026, 7, 1, 10, 0), trigger_price=150.0,
        sl=149.5, tp=151.0, rr=2.0, snapshot_id=None,
        risk_gate_result=None, spread_pips=1.2,
    )
    row = store.get_shadow_trigger_by_id(trig_id)  # get_shadow_trigger は plan_id 用
    assert row.spread_pips == pytest.approx(1.2)


def test_spread_pips_optional_backward_compat(store):
    trig_id = store.record_shadow_trigger(
        plan_id=2, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=datetime(2026, 7, 1, 10, 0), trigger_price=150.0,
        sl=149.5, tp=151.0, rr=2.0, snapshot_id=None, risk_gate_result=None,
    )
    row = store.get_shadow_trigger_by_id(trig_id)
    assert row.spread_pips is None


def test_migrate_is_idempotent(store, tmp_path):
    """_migrate が 2 回目の構築でも例外を出さない (冪等 ALTER)。"""
    again = OrchestratorStore(tmp_path / "orch.db")
    assert again is not None
