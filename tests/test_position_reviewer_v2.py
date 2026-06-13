"""position_reviewer v2 Time Stop behavior."""
from __future__ import annotations

from datetime import timedelta

from src.trading.position_reviewer import review_open_positions
from src.utils.clock import db_now


def test_time_stop_closes_after_max_holding_hours(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=13)
    buy_order.max_favorable_r = 1.0

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.8},
        time_stop_enabled=True,
        max_holding_hours=12,
    )

    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout"
    assert "13.0h" in decisions[0].detail


def test_time_stop_no_progress_uses_mfe_r_not_tp_progress(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=5)
    buy_order.max_favorable_r = 0.1

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.8},  # TP progress can be good, MFE R is source for no-progress
        time_stop_enabled=True,
        max_holding_hours=12,
        no_progress_hours=4,
        no_progress_min_mfe_r=0.2,
    )

    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout"
    assert "mfe_r=0.10" in decisions[0].detail


def test_time_stop_no_progress_skips_when_mfe_r_sufficient(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=5)
    buy_order.max_favorable_r = 0.25

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.1},
        time_stop_enabled=True,
        max_holding_hours=12,
        no_progress_hours=4,
        no_progress_min_mfe_r=0.2,
        max_holding_days=10,
    )

    assert decisions == []


def test_time_stop_timeout_only_runs_during_halt(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=13)
    buy_order.max_favorable_r = 1.0

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.8},
        timeout_only=True,
        time_stop_enabled=True,
        max_holding_hours=12,
    )

    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout"


def _reversal_signal(pair="USDJPY=X"):
    from datetime import datetime
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from src.signals.signal_combiner import TradeSignal

    news = NewsSentiment(pair=pair, sentiment_score=-0.3, confidence=0.8)
    price = PriceAnalysis(
        pair=pair,
        direction_bias="short",
        bias_score=-0.3,
        confidence=0.8,
        entry_zone=(149.5, 150.5),
        stop_loss=152.0,
        take_profit=148.0,
        risk_reward_ratio=2.0,
        reasoning_summary="test",
        analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair,
        action="sell",
        predicted_direction="bearish",
        combined_score=-0.3,
        confidence=0.8,
        entry_price=150.0,
        stop_loss=152.0,
        take_profit=148.0,
        position_size=10000.0,
        signal_reason="test",
        detail_reason="test",
        news=news,
        price=price,
        generated_at=datetime.now(),
    )


def test_reversal_guard_raises_sl_in_profit_when_close_disabled(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(minutes=300)

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _reversal_signal()},
        current_prices={"USDJPY=X": 150.5},
        reversal_close_enabled=False,
        reversal_raise_sl_to_breakeven=True,
    )

    assert len(decisions) == 1
    assert decisions[0].action == "raise_sl"
    assert decisions[0].target_sl == buy_order.entry_price
    assert decisions[0].close_reason == "reversal_guard"


def test_reversal_guard_does_not_close_losing_position_by_default(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(minutes=300)

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _reversal_signal()},
        current_prices={"USDJPY=X": 149.5},
        reversal_close_enabled=False,
    )

    assert decisions == []


def test_reversal_close_requires_explicit_enable(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(minutes=300)

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _reversal_signal()},
        current_prices={"USDJPY=X": 149.5},
        reversal_close_enabled=True,
    )

    assert len(decisions) == 1
    assert decisions[0].action == "close"
    assert decisions[0].close_reason == "reversal"
