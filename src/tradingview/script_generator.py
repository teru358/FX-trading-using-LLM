"""financeシステムの発注情報からPine Scriptを生成する。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.analysis.prompt_loader import render_prompt


@dataclass
class SignalData:
    """1銘柄分のシグナルデータ。"""
    pair: str                           # 表示名 (例: "USD/JPY")
    tv_ticker: str                      # TradingView ticker (例: "USDJPY")
    direction: str
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    bias_score: float = 0.0
    trend_direction: str = "sideways"
    key_support: float | None = None
    key_resistance: float | None = None
    swing_highs: list[float] = field(default_factory=list)
    swing_lows: list[float] = field(default_factory=list)
    patterns: str = ""


def generate_signal_pine(
    pair: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    reason: str,
    bias_score: float = 0.0,
    trend_direction: str = "sideways",
    key_support: float | None = None,
    key_resistance: float | None = None,
    swing_highs: list[float] | None = None,
    swing_lows: list[float] | None = None,
    patterns: str = "",
) -> str:
    """単一銘柄のPine Script indicatorを生成する（後方互換）。"""
    return render_prompt(
        "pine_signal.j2",
        pair=pair,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
        reason=reason,
        bias_score=bias_score,
        trend_direction=trend_direction,
        key_support=key_support,
        key_resistance=key_resistance,
        swing_highs=swing_highs or [],
        swing_lows=swing_lows or [],
        patterns=patterns,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def generate_multi_signal_pine(signals: list[SignalData]) -> str:
    """複数銘柄のシグナルを1つのPine Scriptに統合する。

    syminfo.tickerで現在のチャートシンボルを判定し、
    該当銘柄のラインを表示する。
    """
    return render_prompt(
        "pine_multi_signal.j2",
        signals=signals,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
