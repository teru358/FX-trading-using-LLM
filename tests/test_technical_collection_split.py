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

    async def _fake_collect_one(inst, config, store, price_store, analysis_store,
                                llm, macro_context="", correlation_context="",
                                price_provider=None, price_data=None):
        analysis_store.add_sentinel(symbol=inst.symbol, status="failed",
                                    reason=f"stub collected macro={bool(macro_context)} "
                                           f"corr={bool(correlation_context)}")

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


def _watch_ohlcv_df(n: int = 60):
    """相関計算に十分なバー数 (>= rolling_window + 5 = 25) の DataFrame。"""
    end = db_now() - timedelta(minutes=15)
    idx = pd.date_range(end=end, periods=n, freq="1h")
    closes = [150.0 + (j % 7) * 0.1 for j in range(n)]
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes,
         "Close": closes, "Volume": [1000] * n},
        index=idx,
    )


def test_collect_trade_reloads_watch_prices_from_pricestore(tmp_path, monkeypatch):
    """collect_trade_technical は trade を収集し、watch 価格を PriceStore.load_ohlcv で
    再ロードして compute_correlations に渡す。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = _watch_ohlcv_df()

    captured = {}

    def _fake_corr(trade_prices, watch_prices, watch_names, **kw):
        captured["watch_symbols"] = sorted(watch_prices.keys())
        captured["trade_symbols"] = sorted(trade_prices.keys())
        return []

    monkeypatch.setattr(tc, "compute_correlations", _fake_corr)
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert store.get_latest_collect_row("SPY") is None
    assert captured["watch_symbols"] == ["SPY"]
    assert captured["trade_symbols"] == ["USDJPY=X"]
    price_store.load_ohlcv.assert_called()


def test_collect_trade_skips_watch_with_missing_prices(tmp_path, monkeypatch):
    """watch 価格が prices.db に無い (空 df) 場合、trade 収集は継続し相関から除外する。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = pd.DataFrame()

    captured = {}

    def _fake_corr(trade_prices, watch_prices, watch_names, **kw):
        captured["watch_symbols"] = sorted(watch_prices.keys())
        return []

    monkeypatch.setattr(tc, "compute_correlations", _fake_corr)
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert captured["watch_symbols"] == []


def test_collect_all_runs_both_watch_and_trade(tmp_path, monkeypatch):
    """collect_all_technical wrapper は watch + trade 両方を収集する (後方互換)。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = _watch_ohlcv_df()
    monkeypatch.setattr(tc, "compute_correlations", lambda *a, **kw: [])
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_all_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("SPY") is not None
    assert store.get_latest_collect_row("USDJPY=X") is not None


def test_collect_trade_excludes_stale_watch_from_correlation(tmp_path, monkeypatch):
    """watch の最新バーが stale (閾値超) なら相関入力から除外される。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]  # index → watch staleness 閾値 120h
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    n = 60
    old_end = db_now() - timedelta(hours=200)
    idx = pd.date_range(end=old_end, periods=n, freq="1h")
    closes = [400.0 + (j % 5) * 0.5 for j in range(n)]
    stale_df = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes,
         "Close": closes, "Volume": [1000] * n}, index=idx,
    )
    price_store = MagicMock()
    price_store.load_ohlcv.return_value = stale_df

    captured = {}

    def _fake_corr(trade_prices, watch_prices, watch_names, **kw):
        captured["watch_symbols"] = sorted(watch_prices.keys())
        return []

    monkeypatch.setattr(tc, "compute_correlations", _fake_corr)
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert captured["watch_symbols"] == []
