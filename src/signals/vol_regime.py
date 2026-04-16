"""ボラレジーム判定 + EWMA ベース position sizing スケーリング。

ATR(14) の EWMA を計算し、現在の ATR が EWMA に対して
high / normal / low のどのレジームにいるかを判定する。
high vol → risk を縮小、low vol → risk を維持 (デフォルト)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VolRegimeResult:
    """ボラレジーム判定結果。"""
    regime: str          # "low" | "normal" | "high"
    atr: float           # 直近の ATR(14) 値
    ewma_atr: float      # ATR の EWMA 値
    ratio: float         # atr / ewma_atr
    risk_scale: float    # risk_per_trade に掛ける倍率


def compute_vol_regime(
    ohlcv_df,
    *,
    ewma_span: int = 20,
    high_threshold: float = 1.3,
    low_threshold: float = 0.7,
    high_risk_scale: float = 0.5,
    low_risk_scale: float = 1.0,
) -> VolRegimeResult | None:
    """OHLCV DataFrame からボラレジームを判定する。

    Args:
        ohlcv_df: pandas DataFrame (Open/High/Low/Close/Volume, datetime index)
        ewma_span: EWMA の期間 (bar 数)
        high_threshold: ATR/EWMA がこの値以上 → high vol
        low_threshold: ATR/EWMA がこの値以下 → low vol
        high_risk_scale: high vol 時の risk 倍率 (< 1.0 で縮小)
        low_risk_scale: low vol 時の risk 倍率

    Returns:
        VolRegimeResult or None (データ不足時)
    """
    import pandas as pd

    if ohlcv_df is None or len(ohlcv_df) < ewma_span + 14:
        return None

    try:
        import pandas_ta as pta
        atr_series = pta.atr(
            ohlcv_df["High"], ohlcv_df["Low"], ohlcv_df["Close"], length=14,
        )
        if atr_series is None or atr_series.dropna().empty:
            return None

        ewma = atr_series.ewm(span=ewma_span, adjust=False).mean()

        current_atr = float(atr_series.iloc[-1])
        current_ewma = float(ewma.iloc[-1])

        if current_ewma <= 0:
            return None

        ratio = current_atr / current_ewma

        if ratio >= high_threshold:
            regime = "high"
            risk_scale = high_risk_scale
        elif ratio <= low_threshold:
            regime = "low"
            risk_scale = low_risk_scale
        else:
            regime = "normal"
            risk_scale = 1.0

        return VolRegimeResult(
            regime=regime,
            atr=current_atr,
            ewma_atr=current_ewma,
            ratio=round(ratio, 3),
            risk_scale=risk_scale,
        )
    except Exception as e:
        logger.warning(f"[VOL-REGIME] computation failed: {e}")
        return None
