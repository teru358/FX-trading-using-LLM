"""HindsightEvaluator の pure 計算テスト (plan Phase 4 Task 4.2)。

trigger_price 起点で OHLCV から MFE-R / MAE-R / PnL-R を後追い計測する。
1R = |trigger_price - sl| (価格距離) を基準に R 正規化する。direction に応じて
順行 / 逆行の符号を調整する。発注はしない (shadow)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.orchestrator.hindsight_evaluator import HindsightEvaluator, HindsightResult

TRIG = datetime(2026, 6, 21, 9, 0, 0)


def _ohlcv(bars: list[tuple[datetime, float, float, float]]) -> pd.DataFrame:
    """(time, high, low, close) のリストから OHLCV DataFrame を作る。"""
    return pd.DataFrame(
        {
            "Open": [b[3] for b in bars],
            "High": [b[1] for b in bars],
            "Low": [b[2] for b in bars],
            "Close": [b[3] for b in bars],
            "Volume": [0 for _ in bars],
        },
        index=pd.to_datetime([b[0] for b in bars]),
    )


def _evaluator(df: pd.DataFrame) -> HindsightEvaluator:
    def provider(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        # 実 load_ohlcv は SQL で窓を切って返す。空フレームは DatetimeIndex を持たない
        # ことがある (load_ohlcv の columns-only 空フレーム) ため、空ならそのまま返す。
        if len(df) == 0:
            return df
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    return HindsightEvaluator(ohlcv_provider=provider)


def test_long_hits_tp_pnl_is_positive_r() -> None:
    """long で TP に到達 → PnL-R = (tp-trigger)/R、would_hit_tp=True。"""
    # trigger=150.0, sl=149.0 → 1R = 1.0。tp=152.0 → +2R。
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 150.5, 149.8, 150.3),
        (TRIG + timedelta(hours=2), 152.2, 150.2, 152.1),  # High が TP 超え
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert isinstance(res, HindsightResult)
    assert res.would_hit_tp is True
    assert res.would_hit_sl is False
    assert res.pnl_r == pytest.approx(2.0)
    assert res.mfe_r == pytest.approx(2.2)   # (152.2-150.0)/1.0
    assert res.mae_r == pytest.approx(-0.2)  # (149.8-150.0)/1.0


def test_long_hits_sl_pnl_is_negative_one_r() -> None:
    """long で SL に到達 → PnL-R = -1R、would_hit_sl=True。"""
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 150.1, 148.9, 148.95),  # Low が SL 割れ
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.would_hit_sl is True
    assert res.pnl_r == pytest.approx(-1.0)
    assert res.mae_r == pytest.approx(-1.1)  # (148.9-150.0)/1.0


def test_long_no_hit_marks_to_market_at_horizon() -> None:
    """SL/TP どちらも未到達 → 最終バー close で mark-to-market した PnL-R。"""
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 150.4, 149.7, 150.2),
        (TRIG + timedelta(hours=2), 150.6, 149.9, 150.5),  # 最終 close=150.5
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.would_hit_sl is False
    assert res.would_hit_tp is False
    assert res.pnl_r == pytest.approx(0.5)   # (150.5-150.0)/1.0
    assert res.mfe_r == pytest.approx(0.6)
    assert res.mae_r == pytest.approx(-0.3)


def test_short_hits_tp() -> None:
    """short で価格が tp(下) に到達 → would_hit_tp=True, PnL-R 正。

    short は下落が順行。trigger=150, sl=151 → 1R=1.0。
    """
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 150.2, 147.9, 148.0),  # Low 147.9 <= tp 148.0
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="short",
        trigger_price=150.0, sl=151.0, tp=148.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.would_hit_tp is True
    assert res.would_hit_sl is False
    assert res.pnl_r == pytest.approx(2.0)   # (150.0-148.0)/1.0
    assert res.mfe_r == pytest.approx(2.1)   # (150.0-147.9)/1.0


def test_short_no_hit_marks_to_market() -> None:
    """short 未到達 → mark-to-market。下落 = 利益。"""
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 150.3, 149.4, 149.5),  # close 149.5
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="short",
        trigger_price=150.0, sl=151.0, tp=148.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.would_hit_tp is False
    assert res.pnl_r == pytest.approx(0.5)   # (150.0-149.5)/1.0
    assert res.mae_r == pytest.approx(-0.3)  # 逆行 (上昇) 150.3 → -0.3R


def test_no_ohlcv_returns_none_result() -> None:
    """horizon 内に OHLCV が無い → 評価不能を示す (has_data=False)。"""
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    res = _evaluator(empty).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.has_data is False
    assert res.pnl_r is None


def test_sl_before_tp_uses_conservative_sl() -> None:
    """バー内で SL/TP 両方 touch しうる場合、不利側 (SL) を保守的に採用する。"""
    df = _ohlcv([
        (TRIG + timedelta(hours=1), 152.1, 148.9, 150.0),  # High>TP かつ Low<SL
    ])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.would_hit_sl is True
    assert res.pnl_r == pytest.approx(-1.0)  # 不利側 (SL) を採用


def test_zero_risk_distance_returns_failed() -> None:
    """trigger_price == sl (R=0) → 0 除算を避け has_data=False で返す。"""
    df = _ohlcv([(TRIG + timedelta(hours=1), 150.5, 149.5, 150.2)])
    res = _evaluator(df).evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=150.0, tp=152.0,
        triggered_at=TRIG, horizon_seconds=86400,
    )
    assert res.has_data is False
    assert res.pnl_r is None
