"""DecisionContextBuilder の technical stale 判定が config から導出されることのテスト
(codex Med#2)。

class 定数ハードコード (30min 固定) だと、day config で
orchestrator.entry.max_technical_age_seconds=5400 (90min) を設定しても stale 閾値が
追従せず、健全な 31-89min 経過 snapshot が stale 誤判定されて plan が構造的に reject
される。entry.max_technical_age_seconds から導出することで watch loop の freshness gate
と基準を統一する。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.config.schema import OrchestratorConfig, OrchestratorEntryConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot


def _as_snapshot(price) -> TechnicalSnapshotData:
    return TechnicalSnapshotData(
        pair=price.pair, analyzed_at=price.analyzed_at,
        bias_score=price.bias_score, confidence=price.confidence,
        direction_bias=price.direction_bias,
    )


def _make_builder(tmp_path: Path, config: OrchestratorConfig) -> DecisionContextBuilder:
    db = tmp_path / "orch.db"
    return DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=config,
    )


def test_default_config_marks_45min_old_snapshot_as_stale(tmp_path: Path, bullish_price) -> None:
    """既定 config (max_technical_age_seconds=1800=30min) では 45 分前の ok snapshot は stale。"""
    builder = _make_builder(tmp_path, OrchestratorConfig())
    bullish_price.analyzed_at = datetime.now() - timedelta(minutes=45)
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "stale"


def test_extended_max_technical_age_marks_45min_old_snapshot_as_ok(
    tmp_path: Path, bullish_price
) -> None:
    """max_technical_age_seconds=5400 (90min, day config 相当) では同じ 45 分前 snapshot が ok。"""
    config = OrchestratorConfig(
        entry=OrchestratorEntryConfig(max_technical_age_seconds=5400),
    )
    builder = _make_builder(tmp_path, config)
    bullish_price.analyzed_at = datetime.now() - timedelta(minutes=45)
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "ok"
    assert ctx["technical"]["direction"] == "long"
