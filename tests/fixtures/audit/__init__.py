"""audit テスト用 fixture builder。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class FakeSession:
    """_TradingSession を模す軽量 stub。"""
    session_id: str
    pair: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    signal_score: float
    signal_confidence: float
    macro_context: str
    analysis_summary: str
    opened_at: datetime
    closed_at: datetime
    close_price: float
    close_reason: str
    realized_pnl: float
    outcome: str
    reflection_text: str = ""
    atr_value: float = 0.3
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 2.0


def make_fake_session(
    sid: str = "s1",
    pair: str = "USDJPY=X",
    direction: str = "buy",
    pnl: float = 1000.0,
    conf: float = 0.7,
    close_reason: str = "take_profit",
    opened_at: datetime | None = None,
    duration_hours: int = 4,
) -> FakeSession:
    if opened_at is None:
        opened_at = datetime(2026, 4, 1, 10, 0)
    closed_at = opened_at + timedelta(hours=duration_hours)
    return FakeSession(
        session_id=sid,
        pair=pair,
        direction=direction,
        entry_price=150.0,
        stop_loss=149.5,
        take_profit=151.0,
        position_size=10000,
        signal_score=0.3,
        signal_confidence=conf,
        macro_context="test macro",
        analysis_summary="test analysis",
        opened_at=opened_at,
        closed_at=closed_at,
        close_price=150.0 + (pnl / 10000 if direction == "buy" else -pnl / 10000),
        close_reason=close_reason,
        realized_pnl=pnl,
        outcome="win" if pnl > 0 else "loss",
    )
