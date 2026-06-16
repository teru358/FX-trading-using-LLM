"""position_reviewer v2 L2 timeout behavior."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.trading.position_reviewer import review_open_positions
from src.utils.clock import db_now


def _signal(pair="USDJPY=X", direction="bullish", score=0.3, confidence=0.8):
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from src.signals.signal_combiner import TradeSignal

    signed_score = abs(score) if direction == "bullish" else -abs(score)
    news = NewsSentiment(pair=pair, sentiment_score=signed_score, confidence=confidence)
    price = PriceAnalysis(
        pair=pair,
        direction_bias="long" if direction == "bullish" else "short",
        bias_score=signed_score,
        confidence=confidence,
        entry_zone=(149.5, 150.5),
        stop_loss=149.0 if direction == "bullish" else 152.0,
        take_profit=152.0 if direction == "bullish" else 148.0,
        risk_reward_ratio=2.0,
        reasoning_summary="test",
        analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair,
        action="buy" if direction == "bullish" else "sell",
        predicted_direction=direction,
        combined_score=signed_score,
        confidence=confidence,
        entry_price=150.0,
        stop_loss=price.stop_loss,
        take_profit=price.take_profit,
        position_size=10000.0,
        signal_reason="test",
        detail_reason="test",
        news=news,
        price=price,
        generated_at=datetime.now(),
    )


def test_no_progress_watch_does_not_close_before_exit_hours(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=7)
    buy_order.max_favorable_r = 0.03

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 149.9},
        time_stop_enabled=True,
        no_progress_enabled=True,
        no_progress_watch_hours=6,
        no_progress_exit_hours=12,
        no_progress_min_mfe_r=0.1,
    )

    assert decisions == []


def test_no_progress_exit_keeps_position_when_signal_still_supports(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=13)
    buy_order.max_favorable_r = 0.03

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bullish")},
        current_prices={"USDJPY=X": 149.9},
        time_stop_enabled=True,
        no_progress_enabled=True,
        no_progress_watch_hours=6,
        no_progress_exit_hours=12,
        no_progress_min_mfe_r=0.1,
        no_progress_requires_signal_weakness=True,
    )

    assert decisions == []


def test_no_progress_exit_closes_when_signal_is_weak_or_opposite(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=13)
    buy_order.max_favorable_r = 0.03

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bearish")},
        current_prices={"USDJPY=X": 149.9},
        time_stop_enabled=True,
        no_progress_enabled=True,
        no_progress_watch_hours=6,
        no_progress_exit_hours=12,
        no_progress_min_mfe_r=0.1,
        no_progress_requires_signal_weakness=True,
        reversal_close_enabled=False,
    )

    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout_no_progress"
    assert "after 12h" in decisions[0].detail


def test_no_progress_timeout_only_does_not_close_without_signal_by_default(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=13)
    buy_order.max_favorable_r = 0.03

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 149.9},
        timeout_only=True,
        time_stop_enabled=True,
        no_progress_enabled=True,
        no_progress_exit_hours=12,
        no_progress_requires_signal_weakness=True,
    )

    assert decisions == []


def test_stale_position_review_keeps_supported_or_progressing_position(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=25)
    buy_order.max_favorable_r = 0.6

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bullish")},
        current_prices={"USDJPY=X": 150.5},
        time_stop_enabled=True,
        stale_position_review_hours=24,
        no_progress_min_mfe_r=0.1,
    )

    assert decisions == []


def test_stale_position_review_closes_weak_poor_progress_position(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(hours=25)
    buy_order.max_favorable_r = 0.03

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bearish")},
        current_prices={"USDJPY=X": 149.9},
        time_stop_enabled=True,
        stale_position_review_hours=24,
        no_progress_min_mfe_r=0.1,
        reversal_close_enabled=False,
    )

    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout_stale_position"
    assert "stale" in decisions[0].detail


def test_reversal_guard_raises_sl_in_profit_when_close_disabled(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(minutes=300)

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bearish")},
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
        signals_by_pair={"USDJPY=X": _signal(direction="bearish")},
        current_prices={"USDJPY=X": 149.5},
        reversal_close_enabled=False,
    )

    assert decisions == []


def test_reversal_close_requires_explicit_enable(buy_order) -> None:
    buy_order.opened_at = db_now() - timedelta(minutes=300)

    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": _signal(direction="bearish")},
        current_prices={"USDJPY=X": 149.5},
        reversal_close_enabled=True,
    )

    assert len(decisions) == 1
    assert decisions[0].action == "close"
    assert decisions[0].close_reason == "reversal"
