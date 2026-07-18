"""Task F-5 (spec §F-5): plan 作成成功 / 約定成功のターミナルログ。

plan→trigger→execute の一連をログで追えるようにする (mode 非依存)。
- plan_create の可視化は run_planning_cycle の統一 result ログ ([ORCH] planning
  result) に一本化された。_notify_planning_result からは 📋 plan created を出さない
  (二重ログ排除。start:result 1:1 契約は test_planning_cycle_logging を参照)。
- executed 成功   → [ORCH] ✅ live execute ... (executed は alert 対象外のため別途 INFO)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.planning_pipeline import PipelineResult
from src.orchestrator.runtime import OrchestratorRuntime
from src.trading.broker_adapter import ExecutionResult

from tests.test_taskf_live_execution_helpers import (
    NOW,
    _FakeBroker,
    _GatePass,
    _executed_order,
    make_live_runtime,
    seed_active_plan_ready_to_trigger,
)


def _bare_runtime(tmp_path: Path) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(), orch_store=orch, context_builder=builder,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
    )


def test_notify_planning_result_no_longer_emits_plan_created_log(tmp_path, caplog):
    """📋 plan created は _notify_planning_result からは出さない (統一 result ログへ移設)。

    plan_create 可視化は run_planning_cycle の [ORCH] planning result に一本化され、
    通知本体からは二重ログが消えた (start:result 1:1 契約と二重排除)。
    """
    rt = _bare_runtime(tmp_path)
    result = PipelineResult(
        outcome="plan_create", plan_id=7, direction="long", score=0.42, confidence=0.66,
    )
    with caplog.at_level(logging.INFO, logger="src.orchestrator.runtime"):
        rt._notify_planning_result("USDJPY=X", result)
    assert not [r for r in caplog.records if "📋 plan created" in r.getMessage()]


def test_live_execute_success_emits_info_log(tmp_path, caplog):
    """executed (約定成功) 時に [ORCH] ✅ live execute ... INFO が 1 本出る。"""
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    seed_active_plan_ready_to_trigger(rt)
    with caplog.at_level(logging.INFO, logger="src.orchestrator.runtime"):
        rt.run_watch_cycle(now=NOW)
    executed = [r for r in caplog.records if "✅ live execute" in r.getMessage()]
    assert len(executed) == 1
