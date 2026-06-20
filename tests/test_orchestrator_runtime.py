"""OrchestratorRuntime のループ・trace 記録テスト (observe mode, 発注なし)。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import ContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime


@pytest.fixture
def runtime(tmp_path: Path) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = ContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
    )


def test_run_planning_cycle_records_direct_hold(runtime: OrchestratorRuntime) -> None:
    """1 planning cycle で snapshot + agent_run + direct_hold decision が残る。"""
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    # agent_run が ok で finish している
    run = runtime._orch.get_run(1)
    assert run is not None
    assert run.status == "ok"
    assert run.finished_at is not None

    # direct_hold decision が記録されている
    dec = runtime._orch.get_decision(1)
    assert dec is not None
    assert dec.decision_type == "direct_hold"
    assert dec.pair == "USDJPY=X"
    assert dec.plan_id is None


def test_run_watch_cycle_no_active_plans_is_noop(runtime: OrchestratorRuntime) -> None:
    """active plan が無ければ watch cycle は何も執行せず例外も出さない。"""
    triggered = runtime.run_watch_cycle()
    assert triggered == []


def test_run_watch_cycle_records_freshness_for_active_plan(
    runtime: OrchestratorRuntime,
) -> None:
    """active plan があれば freshness を記録するが、観測モードでは執行しない。"""
    snap_id = runtime._orch.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    runtime._orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap_id, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=1,
    )
    triggered = runtime.run_watch_cycle()
    # observe mode: 条件評価は later plan。ここでは執行 (order) が無いことを保証する。
    assert triggered == []


def test_start_is_noop_when_disabled(runtime: OrchestratorRuntime) -> None:
    """enabled=false (既定) なら start() でスレッドが立たない。"""
    runtime.start()
    assert runtime._planning_thread is None
    assert runtime._watch_thread is None
    runtime.stop()  # 何もしないことを確認 (例外が出ない)
