"""technical_collector の sentinel 書き込みテスト。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.data.analysis_store import AnalysisStore
from src.jobs.technical_collector import _collect_one, _is_price_data_stale
from src.utils.clock import db_now
import src.utils.clock as clock


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
        # FX staleness config 化 (spec S-3): 既定 360min (=6h) と等価にしておく。
        schedule=MagicMock(technical_max_staleness_fx_minutes=360),
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


def _fresh_price_data(symbol: str = "USDJPY=X"):
    """staleness check を通過する fresh PriceData。"""
    bar_time = db_now() - timedelta(minutes=15)
    df = pd.DataFrame(
        {"Open": [150.0] * 100, "High": [150.5] * 100, "Low": [149.5] * 100,
         "Close": [150.0] * 100, "Volume": [1000] * 100},
        index=pd.date_range(end=bar_time, periods=100, freq="1h"),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def test_collect_one_indicator_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """compute_indicators が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score", _raise,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "indicator_error" in (latest.reasoning_summary or "")
    assert "boom" in (latest.reasoning_summary or "")


def test_collect_one_rag_context_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """_build_rag_contexts が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score",
        lambda *a, **kw: (MagicMock(), MagicMock(), None),
    )

    def _raise(*a, **kw):
        raise RuntimeError("rag down")

    monkeypatch.setattr(
        "src.jobs.technical_collector._build_rag_contexts", _raise,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "rag_context_error" in (latest.reasoning_summary or "")


def test_collect_one_llm_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """analyze_price_action が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score",
        lambda *a, **kw: (MagicMock(), MagicMock(), None),
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector._build_rag_contexts",
        lambda *a, **kw: ("", "", ""),
    )

    async def _raise_async(*a, **kw):
        raise TimeoutError("llm timeout")

    monkeypatch.setattr(
        "src.jobs.technical_collector.analyze_price_action", _raise_async,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "llm_error" in (latest.reasoning_summary or "")
    assert "timeout" in (latest.reasoning_summary or "").lower()


def test_collect_all_prefetch_failure_writes_failed_sentinel(tmp_path, monkeypatch):
    """outer loop の prefetch 失敗 → failed sentinel を書く。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector.create_llm_client",
        lambda *a, **kw: MagicMock(model_name="test"),
    )

    def _fetch_fail(*a, **kw):
        raise ConnectionError("bridge down")

    monkeypatch.setattr(
        "src.jobs.technical_collector._fetch_instrument_ohlcv", _fetch_fail,
    )

    asyncio.run(collect_all_technical(
        config=config, store=MagicMock(), price_store=MagicMock(),
        analysis_store=store, force=True,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "prefetch_failed" in (latest.reasoning_summary or "")
    assert "ConnectionError" in (latest.reasoning_summary or "")


def test_collect_all_unexpected_raise_in_collect_one_writes_sentinel(tmp_path, monkeypatch):
    """_collect_one が想定外で raise しても outer loop が sentinel を書く保険。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector.create_llm_client",
        lambda *a, **kw: MagicMock(model_name="test"),
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector._fetch_instrument_ohlcv",
        lambda *a, **kw: _fresh_price_data(),
    )

    async def _raise_unexpected(*a, **kw):
        raise SystemError("totally unexpected")

    monkeypatch.setattr(
        "src.jobs.technical_collector._collect_one", _raise_unexpected,
    )

    asyncio.run(collect_all_technical(
        config=config, store=MagicMock(), price_store=MagicMock(),
        analysis_store=store, force=True,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "unexpected_raise" in (latest.reasoning_summary or "")


def test_collect_all_phase1_prefetch_failure_writes_failed_sentinel(tmp_path, monkeypatch):
    """Phase 1 (watch_only) でも prefetch 失敗時に failed sentinel が書かれる。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = [_inst(symbol="SPY", asset_type="index")]
    config.tradeable_instruments = []
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector.create_llm_client",
        lambda *a, **kw: MagicMock(model_name="test"),
    )

    def _fetch_fail(*a, **kw):
        raise ConnectionError("twelvedata down")

    monkeypatch.setattr(
        "src.jobs.technical_collector._fetch_instrument_ohlcv", _fetch_fail,
    )

    asyncio.run(collect_all_technical(
        config=config, store=MagicMock(), price_store=MagicMock(),
        analysis_store=store, force=True,
    ))

    latest = store.get_latest_collect_row("SPY")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "prefetch_failed" in (latest.reasoning_summary or "")
    assert "ConnectionError" in (latest.reasoning_summary or "")


_JST_TC = ZoneInfo("Asia/Tokyo")


def test_is_price_data_stale_raw_aware_utc_fresh_not_stale(monkeypatch):
    """生の tz-aware UTC の新鮮なバーは stale 判定されない (剥がすだけバグの回帰)。"""
    monkeypatch.setattr(clock, "_resolve_local_tz", lambda tz=None: _JST_TC)
    now_local = db_now()
    aware_utc = (
        pd.Timestamp(now_local).tz_localize(_JST_TC).tz_convert("UTC")
        - pd.Timedelta(minutes=15)
    )
    df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1]},
        index=pd.DatetimeIndex([aware_utc]),  # tz-aware UTC のまま
    )
    price_data = SimpleNamespace(symbol="USDJPY=X", df=df, current_price=1.0)
    assert _is_price_data_stale(price_data, max_staleness=timedelta(hours=6)) is None
