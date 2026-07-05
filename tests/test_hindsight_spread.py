"""hindsight の spread 込み採点 (spec S-5 / D-8): pnl_r は控除後、spread_cost_r に内訳。"""
from datetime import datetime

import pandas as pd
import pytest

from src.orchestrator.hindsight_evaluator import HindsightEvaluator


def _provider_factory(df):
    return lambda pair, start, end: df


def _df_flat(close=150.0, n=8):
    idx = pd.date_range("2026-07-01 10:00", periods=n, freq="1h")
    return pd.DataFrame(
        {"Open": [close] * n, "High": [close + 0.1] * n,
         "Low": [close - 0.1] * n, "Close": [close] * n, "Volume": [0.0] * n},
        index=idx,
    )


def _evaluate(spread_pips):
    ev = HindsightEvaluator(ohlcv_provider=_provider_factory(_df_flat()))  # ctor は keyword-only
    return ev.evaluate(
        pair="USDJPY=X", direction="long", trigger_price=150.0,
        sl=149.5, tp=151.0,
        triggered_at=datetime(2026, 7, 1, 10, 0), horizon_seconds=3600 * 8,
        spread_pips=spread_pips,
    )


def test_spread_cost_deducted_from_pnl_r():
    # risk = 0.5, USDJPY pip = 0.01。spread 5pips = 0.05 → cost_r = 0.1
    with_spread = _evaluate(5.0)
    without = _evaluate(None)
    assert with_spread.spread_cost_r == pytest.approx(0.1)
    assert with_spread.pnl_r == pytest.approx(without.pnl_r - 0.1)


def test_no_spread_keeps_gross_and_zero_cost():
    res = _evaluate(None)
    assert res.spread_cost_r is None
    # mark-to-market フラットなので gross pnl_r ≈ 0
    assert res.pnl_r == pytest.approx(0.0, abs=1e-9)


def test_zero_spread_treated_as_no_cost():
    res = _evaluate(0.0)
    assert res.spread_cost_r is None
    assert res.pnl_r == pytest.approx(0.0, abs=1e-9)
