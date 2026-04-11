"""trading_cycle.py のヘルパー関数ユニットテスト。

_fetch_and_compute_atr / _apply_atr_sltp_to_signal が fetch_ohlcv / _get_ohlcv に
渡す period 引数が正しい str 形式 ("90d") であることを保証する。

Regression guard: 以前 config.trading.lookback_days(int) をそのまま渡しており、
fetch_ohlcv の _parse_period_days が AttributeError で落ちて ATR が常に None に
なっていた。本テストはその退行を検知する。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.price_fetcher import PriceData


def _make_price_data(symbol: str = "USDJPY=X", bars: int = 20) -> PriceData:
    """pandas_ta.atr(14) が計算できる最低限の OHLCV を返す。"""
    idx = pd.date_range("2026-04-01", periods=bars, freq="1h")
    df = pd.DataFrame(
        {
            "Open":   [150.0 + i * 0.1 for i in range(bars)],
            "High":   [150.5 + i * 0.1 for i in range(bars)],
            "Low":    [149.5 + i * 0.1 for i in range(bars)],
            "Close":  [150.2 + i * 0.1 for i in range(bars)],
            "Volume": [1000] * bars,
        },
        index=idx,
    )
    return PriceData(
        symbol=symbol,
        df=df,
        current_price=float(df["Close"].iloc[-1]),
        fetched_at=datetime.now(),
    )


# ── _fetch_and_compute_atr ────────────────────────────────────


def test_fetch_and_compute_atr_passes_string_period(monkeypatch):
    """_fetch_and_compute_atr は fetch_ohlcv に str 形式の period を渡すこと。"""
    from src.config import load_config
    from src.trading_cycle import _fetch_and_compute_atr

    captured: dict = {}

    def fake_fetch(symbol, period, interval, price_store=None):
        captured["period"] = period
        return _make_price_data(symbol)

    monkeypatch.setattr("src.data.price_fetcher.fetch_ohlcv", fake_fetch)

    config = load_config()

    class _DummyStore:
        pass

    atr = _fetch_and_compute_atr("USDJPY=X", config, _DummyStore())

    # period が str として渡っていること (int だと fetch_ohlcv 内部で AttributeError)
    assert isinstance(captured.get("period"), str), (
        f"period must be str, got {type(captured.get('period')).__name__}"
    )
    assert captured["period"].endswith("d"), (
        f"period must be like '90d', got {captured['period']!r}"
    )

    # ATR が計算されて戻ってくること (bug 時は None)
    assert atr is not None, "ATR should not be None when valid OHLCV is returned"
    assert atr > 0


def test_fetch_and_compute_atr_none_when_no_store():
    from src.config import load_config
    from src.trading_cycle import _fetch_and_compute_atr

    config = load_config()
    assert _fetch_and_compute_atr("USDJPY=X", config, None) is None


# ── _apply_atr_sltp_to_signal ─────────────────────────────────


def test_apply_atr_sltp_to_signal_uses_string_period(monkeypatch):
    """_apply_atr_sltp_to_signal も同様に str 形式 period を要求する。"""
    from src.config import load_config
    from src.persistence.adaptive_params_store import AdaptiveParamsStore
    from src.trading_cycle import _apply_atr_sltp_to_signal

    captured: dict = {}

    def fake_fetch(symbol, period, interval, price_store=None):
        captured["period"] = period
        return _make_price_data(symbol)

    # trading_cycle は `from src.data.price_fetcher import fetch_ohlcv` で
    # ローカル binding しているため、そちらを直接 patch する必要がある。
    monkeypatch.setattr("src.trading_cycle.fetch_ohlcv", fake_fetch)

    config = load_config()

    # 最小限の signal / position_mgr / adaptive_store モック
    class _Price:
        entry_zone = (150.0, 150.5)
        key_support = 149.0
        key_resistance = 151.0

    class _Signal:
        pair = "USDJPY=X"
        action = "buy"
        entry_price = 150.3
        stop_loss = 149.0
        take_profit = 152.0
        position_size = 1000.0
        combined_score = 0.5
        confidence = 0.8
        price = _Price()

    class _Account:
        balance = 10000.0

    class _PM:
        def get_account_state(self):
            return _Account()

    adaptive_store = AdaptiveParamsStore(
        state_dir=config.state_dir,
        defaults={
            "sl_atr_mult": config.trading.sl_atr_mult_default,
            "tp_atr_mult": config.trading.tp_atr_mult_default,
        },
        limits={
            "sl_atr_mult_min": config.trading.sl_atr_mult_min,
            "sl_atr_mult_max": config.trading.sl_atr_mult_max,
            "tp_atr_mult_min": config.trading.tp_atr_mult_min,
            "tp_atr_mult_max": config.trading.tp_atr_mult_max,
        },
    )

    class _DummyStore:
        pass

    result = _apply_atr_sltp_to_signal(
        _Signal(), config, _PM(), _DummyStore(), adaptive_store,
        price_provider=None,
    )

    assert isinstance(captured.get("period"), str), (
        f"period must be str, got {type(captured.get('period')).__name__}"
    )
    assert captured["period"].endswith("d")
    # ATR が valid なら calculate_sl_tp が SLTPResult を返すはず
    assert result is not None, "SLTPResult should not be None when valid OHLCV is returned"
