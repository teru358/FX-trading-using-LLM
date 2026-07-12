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
        inst=inst, config=_config(),
        price_store=MagicMock(), analysis_store=store,
        price_data=price_data,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "stale_price"
    assert "ago" in (latest.reason or "")
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
        inst=_inst(), config=_config(),
        price_store=MagicMock(), analysis_store=store,
        price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "indicator_error" in (latest.reason or "")
    assert "boom" in (latest.reason or "")


def test_collect_one_ok_writes_snapshot(tmp_path, monkeypatch):
    """正常系: フレッシュな価格データ → add_snapshot が呼ばれ ok 行が書かれる。"""
    from src.signals.technical_scorer import TechnicalScore

    store = AnalysisStore(tmp_path / "test.db")

    fake_score = TechnicalScore(
        sma_score=0.5, rsi_score=0.2, macd_score=0.1, ichimoku_score=0.3,
        bb_score=0.0, pattern_score=0.0, adx_factor=1.0,
        total_score=0.4, confidence=0.7, direction="long",
    )
    fake_summary = SimpleNamespace(chart_patterns=["engulfing_bullish"])
    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score",
        lambda *a, **kw: (fake_summary, fake_score, None),
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(),
        price_store=MagicMock(), analysis_store=store,
        price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "ok"
    assert latest.reason is None

    ok_row = store.get_latest_ok_row("USDJPY=X")
    assert ok_row is not None
    assert ok_row.bias_score is not None
    assert ok_row.confidence is not None
    assert ok_row.direction_bias is not None
    # 決定的スコアの新カラムがある (LLM 無し経路)
    assert ok_row.tf_scores_json is not None
    assert ok_row.components_json is not None
    assert ok_row.patterns_json is not None


def test_collect_all_prefetch_failure_writes_failed_sentinel(tmp_path, monkeypatch):
    """outer loop の prefetch 失敗 → failed sentinel を書く。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.schedule.technical_inter_pair_delay_seconds = 0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
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
    assert "prefetch_failed" in (latest.reason or "")
    assert "ConnectionError" in (latest.reason or "")


def test_collect_all_unexpected_raise_in_collect_one_writes_sentinel(tmp_path, monkeypatch):
    """_collect_one が想定外で raise しても outer loop が sentinel を書く保険。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.schedule.technical_inter_pair_delay_seconds = 0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
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
    assert "unexpected_raise" in (latest.reason or "")


def test_collect_all_phase1_prefetch_failure_writes_failed_sentinel(tmp_path, monkeypatch):
    """Phase 1 (watch_only) でも prefetch 失敗時に failed sentinel が書かれる。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = [_inst(symbol="SPY", asset_type="index")]
    config.tradeable_instruments = []
    config.schedule.technical_inter_pair_delay_seconds = 0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
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
    assert "prefetch_failed" in (latest.reason or "")
    assert "ConnectionError" in (latest.reason or "")


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
