"""protect_shadow 以上で保護 worker が runtime に配線される lifecycle テスト (spec §5.2)。

producer と protection_worker が runtime の start/stop で正しく起動・停止することを検証する。
worker は producer.latest を消費するため start は producer→worker、stop は worker→producer の順。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime


def _make_minimal_runtime(tmp_path, *, quote_producer=None, protection_worker=None):
    """enabled=True の最小 valid OrchestratorRuntime を組む。

    test_orchestrator_runtime.test_start_spawns_threads_when_enabled_and_stop_joins を
    モデルに、実 DB / context_builder / stub quote_provider で構築する。producer/worker の
    lifecycle hook 発火確認だけが目的なので detector/pipeline 等は注入しない。
    """
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(enabled=True),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        quote_producer=quote_producer,
        protection_worker=protection_worker,
    )


def test_runtime_starts_and_stops_protection_worker(monkeypatch, tmp_path: Path):
    started = {"prod": False, "worker": False}
    stopped = {"prod": False, "worker": False}

    class _StubProd:
        def start(self): started["prod"] = True
        def stop(self): stopped["prod"] = True
        def latest(self, pair): return None

    class _StubWorker:
        def start(self): started["worker"] = True
        def stop(self): stopped["worker"] = True

    rt = _make_minimal_runtime(
        tmp_path, quote_producer=_StubProd(), protection_worker=_StubWorker(),
    )
    rt.start()
    try:
        assert started["prod"] and started["worker"]
    finally:
        rt.stop()
    assert stopped["prod"] and stopped["worker"]
