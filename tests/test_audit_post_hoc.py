"""audit_post_hoc モジュールのテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.analysis.audit_post_hoc import (
    Counterfactuals,
    LessonCandidate,
    PostHocResult,
    TradeReview,
    compute_counterfactuals,
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
    # MAE sign contract: non-positive (0 allowed when price never went adverse)
    assert result.mae_during_trade <= 0
    assert result.mae_after_close_24h <= 0


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
    # MAE sign contract: non-positive (0 allowed when price never went adverse)
    assert result.mae_during_trade <= 0
    assert result.mae_after_close_24h <= 0


def test_compute_mfe_mae_mae_clamp_never_adverse_buy():
    """BUY で Low が entry を下回らない場合、MAE は 0 にクランプされる。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    # 150.0 entry, すべての bar で Low > 150 (Low = close - 0.1, close >= 150.2)
    df = _make_ohlcv([150.2, 150.3, 150.4, 150.5, 150.6], opened)

    result = compute_mfe_mae(
        direction="buy",
        entry_price=150.0,
        close_price=150.5,
        position_size=10000,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=df,
    )
    # Low.min = 150.1, still > 150.0, so adverse excursion is 0 (positive raw value clamped)
    assert result.mae_during_trade == 0.0
    assert result.mfe_during_trade > 0


def test_compute_mfe_mae_invalid_direction_raises():
    """無効な direction は ValueError。"""
    import pytest
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    df = _make_ohlcv([150.0, 150.2], opened)
    with pytest.raises(ValueError, match="direction"):
        compute_mfe_mae(
            direction="BUY",  # uppercase, not accepted
            entry_price=150.0,
            close_price=150.2,
            position_size=10000,
            opened_at=opened,
            closed_at=closed,
            ohlcv_df=df,
        )


def test_counterfactuals_wider_tp_hits():
    """TP を +0.5 ATR 広げた場合、到達していれば改善と判定。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    # entry=150.0, TP=150.5 (実際 hit), ATR=0.3
    # wider TP = 150.5 + 0.15 = 150.65 / 150.5 + 0.30 = 150.80
    # 期間中 High が 150.70 まで到達 → +0.5 ATR (150.65) は hit, +1.0 ATR (150.80) は not
    df = _make_ohlcv([150.0, 150.3, 150.6, 150.7, 150.5], opened)

    cf = compute_counterfactuals(
        direction="buy",
        entry_price=150.0,
        stop_loss=149.5,
        take_profit=150.5,
        position_size=10000,
        atr_value=0.3,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=df,
    )
    assert cf.tp_plus_0_5_atr_hit is True
    assert cf.tp_plus_0_5_atr_pnl > 0
    assert cf.tp_plus_1_0_atr_hit is False


def test_counterfactuals_tighter_sl_would_recover():
    """SL を 0.5 ATR タイトにしても最終的に回復しない場合は False。"""
    opened = datetime(2026, 4, 1, 10, 0)
    closed = datetime(2026, 4, 1, 14, 0)
    # BUY entry=150.0, SL=149.5, ATR=0.3 → tighter SL = 149.65
    # 期間中 Low が 149.60 (tighter SL にヒット) だが最終的に 149.45 で SL hit
    df = _make_ohlcv([150.0, 149.80, 149.60, 149.55, 149.45], opened)

    cf = compute_counterfactuals(
        direction="buy",
        entry_price=150.0,
        stop_loss=149.5,
        take_profit=150.5,
        position_size=10000,
        atr_value=0.3,
        opened_at=opened,
        closed_at=closed,
        ohlcv_df=df,
    )
    assert cf.sl_minus_0_5_atr_hit is True
    assert cf.tighter_sl_would_recover is False


from src.analysis.audit_post_hoc import compute_vol_percentile


def test_vol_percentile_middle():
    """atr_pct が分布の中央値なら ≒ 50 を返す。"""
    distribution = [0.001 * i for i in range(1, 11)]  # [0.001, 0.002, ..., 0.010]
    pct = compute_vol_percentile(atr_pct=0.0055, distribution=distribution)
    assert 40 <= pct <= 60


def test_vol_percentile_below_min():
    """分布の最小値未満なら 0。"""
    distribution = [0.005, 0.006, 0.007, 0.008, 0.009]
    pct = compute_vol_percentile(atr_pct=0.001, distribution=distribution)
    assert pct == 0.0


def test_vol_percentile_above_max():
    """分布の最大値超なら 100。"""
    distribution = [0.005, 0.006, 0.007, 0.008, 0.009]
    pct = compute_vol_percentile(atr_pct=0.015, distribution=distribution)
    assert pct == 100.0


def test_vol_percentile_empty_distribution():
    """空分布は None を返す。"""
    assert compute_vol_percentile(atr_pct=0.005, distribution=[]) is None


from src.analysis.audit_post_hoc import build_atr_pct_distribution


def test_build_atr_pct_distribution_none_input():
    """None 入力は空リストを返す。"""
    assert build_atr_pct_distribution(None) == []


def test_build_atr_pct_distribution_too_short():
    """atr_period + 1 本未満なら空リストを返す。"""
    import pandas as pd
    # atr_period=14 デフォルト、14 本以下は不足
    idx = pd.date_range("2026-04-01", periods=10, freq="1h")
    df = pd.DataFrame({
        "Open":  [150.0] * 10,
        "High":  [150.5] * 10,
        "Low":   [149.5] * 10,
        "Close": [150.0] * 10,
        "Volume": [0] * 10,
    }, index=idx)
    assert build_atr_pct_distribution(df) == []


def test_build_atr_pct_distribution_happy_path():
    """一定レンジの OHLCV で ATR% が期待値付近になる。"""
    import pandas as pd
    # 20 本の OHLCV、すべて High-Low=1.0, Close=150.0
    # TR = 1.0 (High-Low が最大)
    # ATR(14) = 1.0
    # ATR% = 1.0 / 150.0 ≈ 0.00667
    idx = pd.date_range("2026-04-01", periods=20, freq="1h")
    df = pd.DataFrame({
        "Open":  [150.0] * 20,
        "High":  [150.5] * 20,
        "Low":   [149.5] * 20,
        "Close": [150.0] * 20,
        "Volume": [0] * 20,
    }, index=idx)
    distribution = build_atr_pct_distribution(df, atr_period=14)

    # 20 本 - 13 本の rolling warmup (NaN) = 7 点残る
    assert len(distribution) == 7
    # すべて ≈ 1.0 / 150.0
    expected = 1.0 / 150.0
    for v in distribution:
        assert abs(v - expected) < 1e-9


from src.analysis.audit_post_hoc import assign_flag


def _mk_ph(mfe_intra=0, mae_intra=0, mfe_after=0, mae_after=0, has=True) -> PostHocResult:
    return PostHocResult(
        mfe_during_trade=mfe_intra, mae_during_trade=mae_intra,
        mfe_after_close_24h=mfe_after, mae_after_close_24h=mae_after,
        duration_seconds=3600, has_post_close_data=has,
    )


def _mk_cf(recover=False) -> Counterfactuals:
    return Counterfactuals(
        tp_plus_0_5_atr_hit=False, tp_plus_0_5_atr_pnl=0.0,
        tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=0.0,
        sl_minus_0_5_atr_hit=False, sl_minus_0_5_atr_pnl=0.0,
        tighter_sl_would_recover=recover,
    )


def test_assign_flag_noise():
    """絶対値小 → NOISE。"""
    assert assign_flag(
        realized_pnl=300, signal_confidence=0.8, close_reason="take_profit",
        ph=_mk_ph(), cf=_mk_cf(),
    ) == "NOISE"


def test_assign_flag_clean_win():
    """勝ち + TP hit + MFE 小 (< 50% of realized) → CLEAN_WIN。"""
    assert assign_flag(
        realized_pnl=2000, signal_confidence=0.7, close_reason="take_profit",
        ph=_mk_ph(mfe_after=500), cf=_mk_cf(),
    ) == "CLEAN_WIN"


def test_assign_flag_tight_tp():
    """勝ち + MFE > 1.5 × realized → TIGHT_TP。"""
    assert assign_flag(
        realized_pnl=2000, signal_confidence=0.7, close_reason="take_profit",
        ph=_mk_ph(mfe_after=4000), cf=_mk_cf(),
    ) == "TIGHT_TP"


def test_assign_flag_late_exit():
    """勝ち + TP 以外で close + intra MFE > 1.5× → LATE_EXIT。"""
    assert assign_flag(
        realized_pnl=2000, signal_confidence=0.7, close_reason="manual",
        ph=_mk_ph(mfe_intra=4000), cf=_mk_cf(),
    ) == "LATE_EXIT"


def test_assign_flag_sl_recover():
    """負け + SL hit + tighter SL なら回復 → SL_RECOVER。"""
    assert assign_flag(
        realized_pnl=-2000, signal_confidence=0.7, close_reason="stop_loss",
        ph=_mk_ph(), cf=_mk_cf(recover=True),
    ) == "SL_RECOVER"


def test_assign_flag_conf_miss():
    """負け + 高 conf + 大損 → CONF_MISS。"""
    assert assign_flag(
        realized_pnl=-2000, signal_confidence=0.85, close_reason="manual",
        ph=_mk_ph(), cf=_mk_cf(),
    ) == "CONF_MISS"


def test_assign_flag_clean_loss():
    """負け + SL hit + 高 conf 以下 → CLEAN_LOSS。"""
    assert assign_flag(
        realized_pnl=-2000, signal_confidence=0.65, close_reason="stop_loss",
        ph=_mk_ph(), cf=_mk_cf(),
    ) == "CLEAN_LOSS"


def test_assign_flag_insufficient_data():
    """post-close データなし → INSUFFICIENT_DATA。"""
    assert assign_flag(
        realized_pnl=2000, signal_confidence=0.7, close_reason="take_profit",
        ph=_mk_ph(has=False), cf=_mk_cf(),
    ) == "INSUFFICIENT_DATA"
