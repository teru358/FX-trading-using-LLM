"""technical_collector の sentinel 書き込みテスト。"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.data.analysis_store import AnalysisStore
from src.jobs.technical_collector import _collect_one
from src.utils.clock import db_now


def _inst(symbol: str = "USDJPY=X", asset_type: str = "fx"):
    return SimpleNamespace(
        symbol=symbol,
        display_name="USD/JPY",
        asset_type=asset_type,
        news_categories=["fx"],
    )


def _stale_price_data(symbol: str, hours_ago: float):
    """staleness check で stale 判定される PriceData を作る (FX は 6h 超で stale)。"""
    bar_time = db_now() - timedelta(hours=hours_ago)
    df = pd.DataFrame(
        {"Open": [150.0], "High": [150.5], "Low": [149.5],
         "Close": [150.0], "Volume": [1000]},
        index=pd.DatetimeIndex([bar_time]),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def _config(analysis_lookback_hours: int = 8):
    return MagicMock(
        rag=MagicMock(
            news_lookback_hours=24,
            reflection_lookback_count=3,
            analysis_lookback_hours=analysis_lookback_hours,
        ),
        analysis=MagicMock(),
        paper_provider="twelvedata",
    )


def test_collect_one_stale_writes_stale_price_sentinel(tmp_path):
    """stale data → add_sentinel('stale_price', ...) が呼ばれ、add_snapshot は呼ばれない。"""
    store = AnalysisStore(tmp_path / "test.db")
    inst = _inst()
    price_data = _stale_price_data("USDJPY=X", hours_ago=10)  # FX 6h を超える stale

    asyncio.run(_collect_one(
        inst=inst, config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(),
        price_data=price_data,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "stale_price"
    assert "ago" in (latest.reasoning_summary or "")
    assert store.get_latest_ok_row("USDJPY=X") is None
