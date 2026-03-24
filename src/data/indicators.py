from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd
import pandas_ta as ta

from src.data.price_fetcher import PriceData

if TYPE_CHECKING:
    from src.config import ChartPatternConfig, IndicatorToggleConfig

logger = logging.getLogger(__name__)


@dataclass
class IndicatorSummary:
    sma_20: float
    sma_50: float
    sma_200: float
    ema_12: float
    ema_26: float
    rsi_14: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    bb_upper: float
    bb_lower: float
    bb_pct_b: float
    atr_14: float
    adx_14: float
    current_price: float
    # 一目均衡表
    tenkan_sen: float = 0.0       # 転換線（9期間）
    kijun_sen: float = 0.0        # 基準線（26期間）
    senkou_span_a: float = 0.0    # 先行スパンA（現在位置）
    senkou_span_b: float = 0.0    # 先行スパンB（現在位置）
    chikou_span: float = 0.0      # 遅行スパン（26期間前の終値）
    kumo_top: float = 0.0         # 雲の上限
    kumo_bottom: float = 0.0      # 雲の下限
    price_vs_kumo: str = "unknown"    # "above" | "inside" | "below"
    tk_cross: str = "none"            # "bullish" | "bearish" | "none"
    kumo_color: str = "unknown"       # "green"（上昇雲）| "red"（下降雲）
    ichimoku_signal: str = "neutral"  # 総合シグナル
    # その他
    recent_highs: list[float] = field(default_factory=list)
    recent_lows: list[float] = field(default_factory=list)
    trend_direction: str = "sideways"
    # チャートパターン
    chart_patterns: list[str] = field(default_factory=list)
    pattern_bias: str = "neutral"  # "bullish" | "bearish" | "neutral"


def _compute_ichimoku(df: pd.DataFrame) -> dict:
    """一目均衡表の全要素を計算して返す。pandas-taに依存せず手計算する。"""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # 転換線（9期間の中値）
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    # 基準線（26期間の中値）
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    # 先行スパンA（転換+基準の中値、26期間先行）
    span_a = ((tenkan + kijun) / 2).shift(26)
    # 先行スパンB（52期間の中値、26期間先行）
    span_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    # 遅行スパン（現在の終値を26期間後方にシフト）
    chikou = close.shift(-26)

    def last(series: pd.Series) -> float:
        val = series.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    def prev(series: pd.Series) -> float:
        val = series.iloc[-2] if len(series) >= 2 else series.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    t = last(tenkan)
    k = last(kijun)
    sa = last(span_a)
    sb = last(span_b)
    # 遅行スパンは26期間前の終値（インデックス -27 が現在の遅行位置）
    chikou_val = float(close.iloc[-27]) if len(close) >= 27 else 0.0

    price = float(close.iloc[-1])
    kumo_top = max(sa, sb)
    kumo_bottom = min(sa, sb)

    # 雲に対する価格位置
    if price > kumo_top:
        price_vs_kumo = "above"
    elif price < kumo_bottom:
        price_vs_kumo = "below"
    else:
        price_vs_kumo = "inside"

    # TKクロス（転換線と基準線のクロス判定）
    prev_t = prev(tenkan)
    prev_k = prev(kijun)
    if prev_t <= prev_k and t > k:
        tk_cross = "bullish"   # 転換線が基準線を上抜け
    elif prev_t >= prev_k and t < k:
        tk_cross = "bearish"   # 転換線が基準線を下抜け
    else:
        tk_cross = "none"

    # 雲の色（先行スパンAがBより上 → 上昇雲=green）
    kumo_color = "green" if sa >= sb else "red"

    # 総合シグナル（複数条件の合致度）
    bullish_count = sum([
        price_vs_kumo == "above",
        t > k,
        kumo_color == "green",
        chikou_val > price,  # 遅行スパンが雲上方
    ])
    bearish_count = sum([
        price_vs_kumo == "below",
        t < k,
        kumo_color == "red",
        chikou_val < price,
    ])

    if bullish_count >= 3:
        ichimoku_signal = "strong_bullish"
    elif bullish_count == 2:
        ichimoku_signal = "bullish"
    elif bearish_count >= 3:
        ichimoku_signal = "strong_bearish"
    elif bearish_count == 2:
        ichimoku_signal = "bearish"
    else:
        ichimoku_signal = "neutral"

    return {
        "tenkan_sen": t,
        "kijun_sen": k,
        "senkou_span_a": sa,
        "senkou_span_b": sb,
        "chikou_span": chikou_val,
        "kumo_top": kumo_top,
        "kumo_bottom": kumo_bottom,
        "price_vs_kumo": price_vs_kumo,
        "tk_cross": tk_cross,
        "kumo_color": kumo_color,
        "ichimoku_signal": ichimoku_signal,
    }


def compute_indicators(
    df: pd.DataFrame,
    indicator_cfg: IndicatorToggleConfig | None = None,
    pattern_cfg: ChartPatternConfig | None = None,
) -> tuple[pd.DataFrame, IndicatorSummary]:
    df = df.copy()

    # Moving averages
    if indicator_cfg is None or indicator_cfg.moving_averages:
        df.ta.sma(length=20, append=True)
        df.ta.sma(length=50, append=True)
        df.ta.sma(length=200, append=True)
        df.ta.ema(length=12, append=True)
        df.ta.ema(length=26, append=True)

    # RSI
    if indicator_cfg is None or indicator_cfg.rsi:
        df.ta.rsi(length=14, append=True)

    # MACD
    if indicator_cfg is None or indicator_cfg.macd:
        df.ta.macd(fast=12, slow=26, signal=9, append=True)

    # Bollinger Bands
    if indicator_cfg is None or indicator_cfg.bollinger_bands:
        df.ta.bbands(length=20, std=2, append=True)

    # ATR
    if indicator_cfg is None or indicator_cfg.atr:
        df.ta.atr(length=14, append=True)

    # ADX
    if indicator_cfg is None or indicator_cfg.adx:
        df.ta.adx(length=14, append=True)

    close = df["Close"]

    def safe(col_patterns: list[str], default: float = 0.0) -> float:
        for pat in col_patterns:
            if pat in df.columns:
                val = df[pat].iloc[-1]
                if pd.notna(val):
                    return float(val)
            for col in df.columns:
                if col.upper().startswith(pat.upper()):
                    val = df[col].iloc[-1]
                    if pd.notna(val):
                        return float(val)
        return default

    sma_20 = safe(["SMA_20"])
    sma_50 = safe(["SMA_50"])
    sma_200 = safe(["SMA_200"])
    current_price = float(close.iloc[-1])

    # Trend direction（SMA整列）
    if sma_20 > 0 and sma_50 > 0 and current_price > sma_20 > sma_50:
        trend = "uptrend"
    elif sma_20 > 0 and sma_50 > 0 and current_price < sma_20 < sma_50:
        trend = "downtrend"
    else:
        trend = "sideways"

    # Swing highs/lows
    recent_close = close.iloc[-30:]
    highs, lows = [], []
    for i in range(1, len(recent_close) - 1):
        if recent_close.iloc[i] > recent_close.iloc[i - 1] and recent_close.iloc[i] > recent_close.iloc[i + 1]:
            highs.append(round(float(recent_close.iloc[i]), 5))
        if recent_close.iloc[i] < recent_close.iloc[i - 1] and recent_close.iloc[i] < recent_close.iloc[i + 1]:
            lows.append(round(float(recent_close.iloc[i]), 5))

    bb_upper = safe(["BBU_20", "BBU"])
    bb_lower = safe(["BBL_20", "BBL"])
    bb_pct_b = 0.5
    if bb_upper != bb_lower and bb_upper > 0:
        bb_pct_b = (current_price - bb_lower) / (bb_upper - bb_lower)

    # 一目均衡表（52本以上のデータが必要）
    use_ichimoku = (indicator_cfg is None or indicator_cfg.ichimoku)
    ichi = _compute_ichimoku(df) if (use_ichimoku and len(df) >= 52) else {}

    # チャートパターン検出
    chart_patterns: list[str] = []
    pattern_bias = "neutral"
    if pattern_cfg is not None:
        from src.data.candle_patterns import detect_patterns
        atr_val = safe(["ATRr_14", "ATR_14", "ATR"])
        pattern_result = detect_patterns(
            df,
            cfg=pattern_cfg,
            atr=atr_val,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            sma_20=sma_20,
        )
        chart_patterns = pattern_result.detected
        pattern_bias = pattern_result.bias

    summary = IndicatorSummary(
        sma_20=sma_20,
        sma_50=sma_50,
        sma_200=sma_200,
        ema_12=safe(["EMA_12"]),
        ema_26=safe(["EMA_26"]),
        rsi_14=safe(["RSI_14"]),
        macd_line=safe(["MACD_12_26_9", "MACD"]),
        macd_signal=safe(["MACDs_12_26_9", "MACDs"]),
        macd_histogram=safe(["MACDh_12_26_9", "MACDh"]),
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_pct_b=round(bb_pct_b, 3),
        atr_14=safe(["ATRr_14", "ATR_14", "ATR"]),
        adx_14=safe(["ADX_14", "ADX"]),
        current_price=current_price,
        tenkan_sen=ichi.get("tenkan_sen", 0.0),
        kijun_sen=ichi.get("kijun_sen", 0.0),
        senkou_span_a=ichi.get("senkou_span_a", 0.0),
        senkou_span_b=ichi.get("senkou_span_b", 0.0),
        chikou_span=ichi.get("chikou_span", 0.0),
        kumo_top=ichi.get("kumo_top", 0.0),
        kumo_bottom=ichi.get("kumo_bottom", 0.0),
        price_vs_kumo=ichi.get("price_vs_kumo", "unknown"),
        tk_cross=ichi.get("tk_cross", "none"),
        kumo_color=ichi.get("kumo_color", "unknown"),
        ichimoku_signal=ichi.get("ichimoku_signal", "neutral"),
        recent_highs=highs[-5:],
        recent_lows=lows[-5:],
        trend_direction=trend,
        chart_patterns=chart_patterns,
        pattern_bias=pattern_bias,
    )

    logger.debug(
        f"Indicators: RSI={summary.rsi_14:.1f} MACD={summary.macd_histogram:+.4f} "
        f"ADX={summary.adx_14:.1f} trend={summary.trend_direction} "
        f"ichimoku={summary.ichimoku_signal} price_vs_kumo={summary.price_vs_kumo} "
        f"patterns={summary.chart_patterns}"
    )
    return df, summary
