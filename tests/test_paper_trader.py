"""paper_trader.execute_signal の統合テスト (scale-in フロー含む)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.persistence.balance_snapshot import BalanceSnapshot
from src.persistence.balance_snapshot import write as write_balance
from src.persistence.state_store import StateStore
from src.signals.signal_combiner import TradeSignal
from src.trading.paper_trader import execute_signal
from src.trading.position_manager import Order, PositionManager


def _signal(action: str = "buy", conf: float = 0.75, combined_score: float = 0.50,
            pair: str = "USDJPY=X") -> TradeSignal:
    """テスト用 signal ファクトリ (test_scale_in.py と同種)。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis

    return TradeSignal(
        pair=pair, action=action,
        predicted_direction="bullish" if action == "buy" else "bearish" if action == "sell" else "neutral",
        combined_score=combined_score, confidence=conf,
        entry_price=160.5, stop_loss=160.0, take_profit=161.5, position_size=1000.0,
        signal_reason="test", detail_reason="",
        news=NewsSentiment(
            pair=pair,
            sentiment_score=0.0,
            confidence=0.5,
        ),
        price=PriceAnalysis(
            pair=pair,
            direction_bias="long" if action == "buy" else "short",
            bias_score=0.5 if action == "buy" else -0.5,
            confidence=0.7,
            entry_zone=(160.0, 161.0),
            reasoning_summary="test",
            analyzed_at=datetime(2026, 4, 30, 12, 0),
        ),
        generated_at=datetime(2026, 4, 30, 12, 0),
    )


@pytest.fixture
def mgr(tmp_path):
    store = StateStore(tmp_path)
    write_balance(
        store.state_dir,
        BalanceSnapshot(
            balance=10000.0,
            deposit=10000.0,
            peak_balance=10000.0,
            source="paper",
            fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    return PositionManager(store, context="Test")


def _common_kwargs(scale_in: bool = False) -> dict:
    return dict(
        max_positions_per_pair=2,
        scale_in_enabled=scale_in,
        scale_in_conf_margin=0.05,
        scale_in_score_margin=0.05,
        drawdown_kill_switch_enabled=False,
        drawdown_kill_switch_max_pct=0.10,
    )


def test_execute_signal_creates_order_when_no_existing_position(mgr):
    signal = _signal("buy")
    order = execute_signal(signal, mgr, **_common_kwargs())
    assert order is not None
    assert order.pair == "USDJPY=X"
    assert order.direction == "buy"
    assert order.open_confidence == 0.75
    assert order.open_score == 0.50


def test_execute_signal_skips_when_existing_pos_and_scale_in_disabled(mgr):
    o = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0,
                  open_confidence=0.65, open_score=0.40)
    mgr.open_position(o)
    signal = _signal("buy", conf=0.85, combined_score=0.60)
    order = execute_signal(signal, mgr, **_common_kwargs(scale_in=False))
    assert order is None


def test_execute_signal_creates_scale_in_order_when_allowed(mgr):
    o = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0,
                  open_confidence=0.65, open_score=0.40)
    mgr.open_position(o)
    signal = _signal("buy", conf=0.78, combined_score=0.60)
    order = execute_signal(signal, mgr, **_common_kwargs(scale_in=True))
    assert order is not None
    assert order.open_confidence == 0.78


def test_execute_signal_skips_when_scale_in_rejected(mgr):
    o = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0,
                  open_confidence=0.65, open_score=0.40)
    mgr.open_position(o)
    signal = _signal("buy", conf=0.68, combined_score=0.42)
    order = execute_signal(signal, mgr, **_common_kwargs(scale_in=True))
    assert order is None


def test_execute_signal_skips_when_max_per_pair_reached(mgr):
    for _ in range(2):
        mgr.open_position(Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0,
                                    open_confidence=0.70, open_score=0.50))
    signal = _signal("buy", conf=0.85, combined_score=0.65)
    order = execute_signal(signal, mgr, **_common_kwargs(scale_in=True))
    assert order is None


def test_execute_signal_returns_none_for_hold(mgr):
    signal = _signal("hold")
    order = execute_signal(signal, mgr, **_common_kwargs())
    assert order is None
