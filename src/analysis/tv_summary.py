"""TradingView テクニカル分析サマリーの取得。

tradingview-ta ライブラリ経由でTVのBUY/SELL/NEUTRAL判定を取得し、
LLMの分析結果との矛盾検出に使用する。CDP不要・API枠なし。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# yfinance symbol → tradingview-ta の (exchange, ticker)
_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    "USDJPY=X": ("OANDA", "USDJPY"),
    "EURUSD=X": ("OANDA", "EURUSD"),
    "GBPUSD=X": ("OANDA", "GBPUSD"),
    "AUDUSD=X": ("OANDA", "AUDUSD"),
    "USDCHF=X": ("OANDA", "USDCHF"),
    "USDCAD=X": ("OANDA", "USDCAD"),
}


@dataclass
class TVSummary:
    """TV テクニカル分析サマリー。"""
    recommendation: str   # STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
    buy: int              # BUY判定の指標数
    sell: int             # SELL判定の指標数
    neutral: int          # NEUTRAL判定の指標数
    ma_recommendation: str       # 移動平均線だけの判定
    osc_recommendation: str      # オシレータだけの判定

    @property
    def direction(self) -> str:
        """long / short / neutral に正規化する。"""
        if self.recommendation in ("STRONG_BUY", "BUY"):
            return "long"
        if self.recommendation in ("STRONG_SELL", "SELL"):
            return "short"
        return "neutral"

    @property
    def strength(self) -> float:
        """判定の強度 (0.0-1.0)。同意している指標の割合。"""
        total = self.buy + self.sell + self.neutral
        if total == 0:
            return 0.0
        if self.direction == "long":
            return self.buy / total
        if self.direction == "short":
            return self.sell / total
        return self.neutral / total


def get_tv_summary(symbol: str, interval: str = "1h") -> TVSummary | None:
    """TradingView からテクニカル分析サマリーを取得する。

    Args:
        symbol: yfinance形式のシンボル (例: "USDJPY=X")
        interval: 足種 "1h" / "4h" / "1d" など

    Returns:
        TVSummary or None (未対応シンボル・取得失敗時)
    """
    if symbol not in _SYMBOL_MAP:
        logger.debug(f"[TV-TA] {symbol}: not in symbol map")
        return None

    exchange, ticker = _SYMBOL_MAP[symbol]

    try:
        from tradingview_ta import TA_Handler, Interval
        interval_map = {
            "1h": Interval.INTERVAL_1_HOUR,
            "4h": Interval.INTERVAL_4_HOURS,
            "1d": Interval.INTERVAL_1_DAY,
        }
        tv_interval = interval_map.get(interval, Interval.INTERVAL_1_HOUR)

        handler = TA_Handler(
            symbol=ticker,
            exchange=exchange,
            screener="forex",
            interval=tv_interval,
        )
        analysis = handler.get_analysis()
    except Exception as e:
        logger.warning(f"[TV-TA] {symbol}: fetch failed: {e}")
        return None

    summary = analysis.summary
    return TVSummary(
        recommendation=summary.get("RECOMMENDATION", "NEUTRAL"),
        buy=int(summary.get("BUY", 0)),
        sell=int(summary.get("SELL", 0)),
        neutral=int(summary.get("NEUTRAL", 0)),
        ma_recommendation=analysis.moving_averages.get("RECOMMENDATION", "NEUTRAL"),
        osc_recommendation=analysis.oscillators.get("RECOMMENDATION", "NEUTRAL"),
    )
