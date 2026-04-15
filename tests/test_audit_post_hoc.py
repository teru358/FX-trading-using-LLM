"""audit_post_hoc モジュールのテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.analysis.audit_post_hoc import (
    Counterfactuals,
    LessonCandidate,
    PostHocResult,
    TradeReview,
    compute_mfe_mae,
)


def _make_ohlcv(closes: list[float], start: datetime) -> pd.DataFrame:
    """OHLCV DataFrame を close 値のリストから生成 (1h 足想定)。"""
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "datetime": pd.Timestamp(start + timedelta(hours=i)),
            "Open": c, "High": c + 0.1, "Low": c - 0.1,
            "Close": c, "Volume": 0,
        })
    df = pd.DataFrame(rows).set_index("datetime")
    return df


def test_dataclasses_instantiation():
    """dataclasses が最小引数でインスタンス化できる。"""
    ph = PostHocResult(
        mfe_during_trade=100.0, mae_during_trade=-50.0,
        mfe_after_close_24h=200.0, mae_after_close_24h=-30.0,
        duration_seconds=3600.0, has_post_close_data=True,
    )
    assert ph.has_post_close_data is True

    cf = Counterfactuals(
        tp_plus_0_5_atr_hit=True, tp_plus_0_5_atr_pnl=1500.0,
        tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=800.0,
        sl_minus_0_5_atr_hit=False, sl_minus_0_5_atr_pnl=0.0,
        tighter_sl_would_recover=False,
    )
    assert cf.tp_plus_0_5_atr_hit is True

    lc = LessonCandidate(
        rule_text="test rule",
        rationale="because",
        applicability="all pairs",
    )
    assert lc.hint_used == ""


def test_compute_mfe_mae_buy_normal():
    """BUY トレードで MFE/MAE が正しく計算される。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    df = _make_ohlcv([150.0, 150.3, 150.2, 150.6, 150.5], opened)
    post_closes = [150.5 + i * 0.05 for i in range(24)]
    post_df = _make_ohlcv(post_closes, closed)
    full_df = pd.concat([df, post_df])

    result = compute_mfe_mae(
        direction="buy",
        entry_price=150.0,
        close_price=150.5,
        position_size=10000,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=full_df,
    )
    assert result.has_post_close_data is True
    # entry=150.0, 最高 High ≒ 150.7 (150.6+0.1) → MFE > 0
    assert result.mfe_during_trade > 0
    # post-close は上昇継続 → mfe_after_close_24h > 0
    assert result.mfe_after_close_24h > 0
    assert result.duration_seconds == 4 * 3600


def test_compute_mfe_mae_no_post_close_data():
    """決済後 OHLCV がない場合 has_post_close_data=False。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    df = _make_ohlcv([150.0, 150.3, 150.2, 150.6, 150.5], opened)
    result = compute_mfe_mae(
        direction="buy",
        entry_price=150.0,
        close_price=150.5,
        position_size=10000,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=df,
    )
    assert result.has_post_close_data is False
    assert result.mfe_after_close_24h == 0.0
    assert result.mae_after_close_24h == 0.0


def test_compute_mfe_mae_sell_direction():
    """SELL トレードでは方向が反転することを検証。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    df = _make_ohlcv([150.0, 149.7, 149.8, 149.4, 149.5], opened)
    post_df = _make_ohlcv([149.5 - i * 0.05 for i in range(24)], closed)
    full_df = pd.concat([df, post_df])

    result = compute_mfe_mae(
        direction="sell",
        entry_price=150.0,
        close_price=149.5,
        position_size=10000,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=full_df,
    )
    assert result.has_post_close_data is True
    assert result.mfe_during_trade > 0
    assert result.mfe_after_close_24h > 0
