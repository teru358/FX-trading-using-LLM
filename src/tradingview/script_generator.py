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
) -> str:
    """発注シグナルをPine Script indicatorとして生成する。"""
    return render_prompt(
        "pine_signal.j2",
        pair=pair,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
        reason=reason,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
