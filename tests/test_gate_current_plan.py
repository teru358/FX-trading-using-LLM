"""current_plan ブロックの gate 拡張テスト (gate spec F-1)。

返答待ち (pending_approval) plan も planner から見える — 承認待ちの間に
同 pair の重複 plan を planner が知らずに乱発しないため。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder
from src.utils.clock import db_now


def _builder(tmp_path: Path) -> tuple[DecisionContextBuilder, OrchestratorStore]:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    return DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig()), orch


def _seed_plan(orch, *, status: str) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status=status,
    )


def test_current_plan_sees_pending_approval(tmp_path):
    builder, orch = _builder(tmp_path)
    _seed_plan(orch, status="pending_approval")
    assert builder._build_current_plan("USDJPY=X") is not None


def test_current_plan_ignores_rejected(tmp_path):
    builder, orch = _builder(tmp_path)
    _seed_plan(orch, status="rejected")
    assert builder._build_current_plan("USDJPY=X") is None
