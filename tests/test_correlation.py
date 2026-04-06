"""相関計算モジュールのテスト。"""
import numpy as np
import pandas as pd
import pytest

from src.data.correlation import (
    PairCorrelation,
    _classify_correlation,
    compute_correlations,
    format_correlation_context,
)
from src.data.price_fetcher import PriceData


def _make_price_data(close_values: list[float], symbol: str = "TEST") -> PriceData:
    """テスト用PriceDataを生成する。"""
    from datetime import datetime
    n = len(close_values)
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    df = pd.DataFrame({
        "Open": close_values,
        "High": [v * 1.01 for v in close_values],
        "Low": [v * 0.99 for v in close_values],
        "Close": close_values,
        "Volume": [1000] * n,
    }, index=idx)
    return PriceData(symbol=symbol, df=df, current_price=close_values[-1], fetched_at=datetime.now())


class TestClassifyCorrelation:
    def test_strong_positive(self):
        strength, direction = _classify_correlation(0.85)
        assert strength == "strong"
        assert direction == "positive"

    def test_strong_negative(self):
        strength, direction = _classify_correlation(-0.75)
        assert strength == "strong"
        assert direction == "negative"

    def test_moderate(self):
        strength, direction = _classify_correlation(0.55)
        assert strength == "moderate"
        assert direction == "positive"

    def test_weak(self):
        strength, direction = _classify_correlation(-0.25)
        assert strength == "weak"
        assert direction == "negative"

    def test_none(self):
        strength, direction = _classify_correlation(0.05)
        assert strength == "none"
        assert direction == "neutral"


class TestComputeCorrelations:
    def test_positive_correlation(self):
        """同方向に動く系列は正の相関。"""
        np.random.seed(42)
        base = np.cumsum(np.random.randn(50)) + 100
        trade = _make_price_data(base.tolist(), "USDJPY=X")
        watch = _make_price_data((base * 0.8 + 10).tolist(), "DX-Y.NYB")

        results = compute_correlations(
            {"USDJPY=X": trade},
            {"DX-Y.NYB": watch},
            {"DX-Y.NYB": "US Dollar Index"},
            rolling_window=20,
        )
        assert len(results) == 1
        assert results[0].current_corr > 0.5
        assert results[0].strength in ("strong", "moderate")

    def test_negative_correlation(self):
        """逆方向に動く系列は負の相関。"""
        np.random.seed(42)
        base = np.cumsum(np.random.randn(50)) + 100
        trade = _make_price_data(base.tolist(), "USDJPY=X")
        watch = _make_price_data((-base + 300).tolist(), "GLD")

        results = compute_correlations(
            {"USDJPY=X": trade},
            {"GLD": watch},
            {"GLD": "Gold"},
            rolling_window=20,
        )
        assert len(results) == 1
        assert results[0].current_corr < -0.5

    def test_insufficient_data(self):
        """データ不足時は空リスト。"""
        trade = _make_price_data([100, 101, 102], "USDJPY=X")
        watch = _make_price_data([200, 201, 202], "DX-Y.NYB")

        results = compute_correlations(
            {"USDJPY=X": trade},
            {"DX-Y.NYB": watch},
            {"DX-Y.NYB": "DXY"},
            rolling_window=20,
        )
        assert results == []


class TestFormatCorrelationContext:
    def test_format_output(self):
        corrs = [
            PairCorrelation(
                trade_symbol="USDJPY=X", watch_symbol="DX-Y.NYB",
                watch_display="US Dollar Index",
                current_corr=0.82, prev_corr=0.75, change=0.07,
                strength="strong", direction="positive",
            ),
            PairCorrelation(
                trade_symbol="USDJPY=X", watch_symbol="GLD",
                watch_display="Gold (GLD ETF)",
                current_corr=-0.65, prev_corr=-0.40, change=-0.25,
                strength="moderate", direction="negative",
            ),
        ]
        text = format_correlation_context(corrs, "USDJPY=X")
        assert "Price Correlation" in text
        assert "US Dollar Index" in text
        assert "r=+0.820" in text
        assert "DIVERGING" in text  # Gold change=-0.25

    def test_no_data(self):
        text = format_correlation_context([], "USDJPY=X")
        assert text == ""

    def test_filter_by_trade_symbol(self):
        corrs = [
            PairCorrelation(
                trade_symbol="EURUSD=X", watch_symbol="DX-Y.NYB",
                watch_display="DXY", current_corr=-0.70, prev_corr=-0.65,
                change=-0.05, strength="strong", direction="negative",
            ),
        ]
        # USDJPY向けにはフィルタされて空
        assert format_correlation_context(corrs, "USDJPY=X") == ""
        assert "DXY" in format_correlation_context(corrs, "EURUSD=X")
