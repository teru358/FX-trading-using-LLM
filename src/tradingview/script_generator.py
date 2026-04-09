"""financeシステムの発注情報からPine Scriptを生成する。"""
from __future__ import annotations

from datetime import datetime

from src.analysis.prompt_loader import render_prompt


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
    """発注シグナルをPine Script indicatorとして生成する。

    テクニカル分析データ（サポレジ・スウィング高安・パターン等）を
    含めてチャートに可視化する。
    """
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
