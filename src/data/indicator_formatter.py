from __future__ import annotations

from src.data.indicators import IndicatorSummary
from src.data.price_fetcher import PriceData


def format_for_llm(price_data: PriceData, summary: IndicatorSummary) -> str:
    """テクニカル指標をOllamaプロンプト用テキストに整形する。"""
    df = price_data.df
    recent = df.iloc[-20:]

    rows = []
    for idx, row in recent.iterrows():
        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        rows.append(
            f"{date_str},{row['Open']:.5f},{row['High']:.5f},{row['Low']:.5f},{row['Close']:.5f}"
        )
    ohlcv_block = "\n".join(rows)

    ichi_block = ""
    if summary.tenkan_sen > 0:
        ichi_block = f"""
=== Ichimoku Kinko Hyo (一目均衡表) ===
Tenkan-sen (転換線 9): {summary.tenkan_sen:.5f}
Kijun-sen  (基準線 26): {summary.kijun_sen:.5f}
Senkou Span A (先行A): {summary.senkou_span_a:.5f}
Senkou Span B (先行B): {summary.senkou_span_b:.5f}
Chikou Span  (遅行線): {summary.chikou_span:.5f}
Kumo (雲): Top={summary.kumo_top:.5f} Bottom={summary.kumo_bottom:.5f} Color={summary.kumo_color}
Price vs Kumo: {summary.price_vs_kumo}
TK Cross: {summary.tk_cross}
Ichimoku Signal: {summary.ichimoku_signal}"""

    pattern_block = ""
    if summary.chart_patterns:
        from src.data.candle_patterns import _BULLISH_PATTERNS, _BEARISH_PATTERNS
        bullish = [p for p in summary.chart_patterns if p in _BULLISH_PATTERNS]
        bearish = [p for p in summary.chart_patterns if p in _BEARISH_PATTERNS]
        neutral = [p for p in summary.chart_patterns if p not in _BULLISH_PATTERNS and p not in _BEARISH_PATTERNS]
        pattern_block = f"""
=== Chart Patterns ===
Detected: {', '.join(summary.chart_patterns)}
  Bullish signals: {', '.join(bullish) if bullish else 'none'}
  Bearish signals: {', '.join(bearish) if bearish else 'none'}
  Neutral signals: {', '.join(neutral) if neutral else 'none'}
  Pattern bias: {summary.pattern_bias}"""

    return f"""=== {price_data.symbol} Daily OHLCV (last 20 bars) ===
Date,Open,High,Low,Close
{ohlcv_block}

=== Classic Indicators ===
Price: {summary.current_price:.5f}
SMA(20): {summary.sma_20:.5f} | SMA(50): {summary.sma_50:.5f} | SMA(200): {summary.sma_200:.5f}
EMA(12): {summary.ema_12:.5f} | EMA(26): {summary.ema_26:.5f}
RSI(14): {summary.rsi_14:.1f}
MACD: Line={summary.macd_line:.5f}, Signal={summary.macd_signal:.5f}, Hist={summary.macd_histogram:+.5f}
Bollinger Bands: Upper={summary.bb_upper:.5f}, Lower={summary.bb_lower:.5f}, %B={summary.bb_pct_b:.3f}
ATR(14): {summary.atr_14:.5f}
ADX(14): {summary.adx_14:.1f}
Recent Swing Highs: {summary.recent_highs}
Recent Swing Lows: {summary.recent_lows}
Trend Direction: {summary.trend_direction}{ichi_block}{pattern_block}"""
