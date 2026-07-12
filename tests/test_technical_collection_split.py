"""collect_watch_technical / collect_trade_technical 分割の振る舞いテスト。"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


def _inst(symbol: str, display: str, asset_type: str = "fx"):
    return SimpleNamespace(
        symbol=symbol, display_name=display, asset_type=asset_type,
        news_categories=["fx"],
        base_currency="USD", quote_currency="JPY",
    )


def _fresh_price_data(symbol: str):
    bar_time = db_now() - timedelta(minutes=15)
    df = pd.DataFrame(
        {"Open": [150.0] * 100, "High": [150.5] * 100, "Low": [149.5] * 100,
         "Close": [150.0] * 100, "Volume": [1000] * 100},
        index=pd.date_range(end=bar_time, periods=100, freq="1h"),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def _split_config(watch, tradeable):
    config = MagicMock()
    config.watch_only_instruments = watch
    config.tradeable_instruments = tradeable
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"
    return config


def _patch_collectible(monkeypatch):
    """prefetch を fresh data に、_collect_one を「行を書くだけ」の stub に差し替える。"""
    import src.jobs.technical_collector as tc

    monkeypatch.setattr(tc, "is_market_open", lambda *a, **kw: True)
    monkeypatch.setattr(tc, "create_llm_client",
                        lambda *a, **kw: MagicMock(model_name="test"))
    monkeypatch.setattr(tc, "_fetch_instrument_ohlcv",
                        lambda inst, *a, **kw: _fresh_price_data(inst.symbol))

    async def _fake_collect_one(inst, config, price_store, analysis_store,
                                price_provider=None, price_data=None):
        analysis_store.add_sentinel(symbol=inst.symbol, status="failed",
                                    reason="stub collected")

    monkeypatch.setattr(tc, "_collect_one", _fake_collect_one)


def test_collect_watch_only_collects_watch_not_trade(tmp_path, monkeypatch):
    """collect_watch_technical は watch のみ収集し、trade symbol には触れない。"""
    from src.jobs.technical_collector import collect_watch_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    asyncio.run(collect_watch_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("SPY") is not None
    assert store.get_latest_collect_row("USDJPY=X") is None


def test_collect_trade_skips_watch_with_missing_prices(tmp_path, monkeypatch):
    """collect_trade_technical は watch には触れず、tradeable のみ収集する。"""
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = pd.DataFrame()

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert store.get_latest_collect_row("SPY") is None


def test_collect_all_runs_both_watch_and_trade(tmp_path, monkeypatch):
    """collect_all_technical wrapper は watch + trade 両方を収集する (後方互換)。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = pd.DataFrame()

    asyncio.run(collect_all_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("SPY") is not None
    assert store.get_latest_collect_row("USDJPY=X") is not None
