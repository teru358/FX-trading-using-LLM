"""ContextBuilder (spec §7 / §8.7) のテスト。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import ContextBuilder, QuoteSnapshot


@pytest.fixture
def builder(tmp_path: Path) -> ContextBuilder:
    db = tmp_path / "orch.db"
    return ContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
    )


def test_build_materializes_snapshot_and_returns_context(builder: ContextBuilder) -> None:
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X",
        now=datetime(2026, 6, 20, 12, 0, 0),
        quote=quote,
    )

    # snapshot_id がコンテキストに含まれ、DB に永続化されている
    assert "snapshot_id" in ctx
    snap = builder._orch.get_snapshot(ctx["snapshot_id"])
    assert snap is not None
    assert snap.pair == "USDJPY=X"

    # §7 標準コンテキストの必須フィールド
    assert ctx["pair"] == "USDJPY=X"
    assert ctx["quote"]["mid"] == 150.01
    assert ctx["technical"]["status"] == "missing"   # AnalysisStore 空
    assert ctx["policy"]["trade_horizon"] == "swing"
    assert ctx["news"]["sentiment_score"] is None


def test_build_reads_fresh_technical_from_store(builder: ContextBuilder, bullish_price) -> None:
    """直近 (max_stale 内) の ok snapshot は status=ok で取り込む。"""
    # bullish_price.analyzed_at は conftest で datetime.now() (≒ 直近) なので fresh。
    builder._analysis.add_snapshot(bullish_price)
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "ok"
    assert ctx["technical"]["direction"] == "long"
    assert ctx["technical"]["bias_score"] == pytest.approx(0.7)


def test_build_falls_to_stale_when_latest_ok_is_too_old(
    builder: ContextBuilder, bullish_price
) -> None:
    """max_stale を超えた古い ok snapshot は status=stale に倒す (古いデータで判断しない)。

    これは設計思想の中核: get_latest_ok_row のような lookback 非依存読みを
    decision context に使うと古いデータ汚染が起きる。ContextBuilder は
    max_stale 窓で stale 判定し、古ければ stale に倒す。
    """
    from datetime import timedelta

    # analyzed_at を max_stale (既定 30min) より十分前にずらして保存する。
    bullish_price.analyzed_at = datetime.now() - timedelta(hours=6)
    builder._analysis.add_snapshot(bullish_price)
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "stale"
    # stale 時は direction/bias を判断材料に使わせない (None に倒す)
    assert ctx["technical"]["direction"] is None
    assert ctx["technical"]["bias_score"] is None
