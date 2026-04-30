"""Scale-in (順張り増し玉) 判定の単体テスト。"""
from __future__ import annotations

from datetime import datetime

from src.signals.scale_in import ScaleInDecision, should_scale_in
from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order


def _make_order(
    direction: str = "buy",
    *,
    open_conf: float | None = 0.65,
    open_score: float | None = 0.40,
    pair: str = "USDJPY=X",
) -> Order:
    """テスト用 Order ファクトリ。"""
    return Order.new(
        pair=pair, direction=direction, entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
        signal_reason="test",
        open_confidence=open_conf, open_score=open_score,
    )


def _make_signal(
    action: str = "buy",
    conf: float = 0.75,
    combined_score: float = 0.50,
    pair: str = "USDJPY=X",
) -> TradeSignal:
    """テスト用 TradeSignal ファクトリ。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis

    return TradeSignal(
        pair=pair, action=action,
        predicted_direction="bullish" if action == "buy" else "bearish" if action == "sell" else "neutral",
        combined_score=combined_score,
        confidence=conf,
        entry_price=160.5, stop_loss=160.0, take_profit=161.5, position_size=1000.0,
        signal_reason="test signal", detail_reason="",
        news=NewsSentiment(
            pair=pair,
            sentiment_score=0.0,
            confidence=0.5,
        ),
        price=PriceAnalysis(
            pair=pair,
            direction_bias="long",
            bias_score=0.5,
            confidence=0.7,
            entry_zone=(160.0, 161.0),
            reasoning_summary="",
            analyzed_at=datetime(2026, 4, 30, 12, 0),
        ),
        generated_at=datetime(2026, 4, 30, 12, 0),
    )


# ── happy path ──
def test_scale_in_allowed_when_both_conf_and_score_exceed_margin():
    existing = [_make_order("buy", open_conf=0.65, open_score=0.40)]
    signal = _make_signal("buy", conf=0.75, combined_score=0.50)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is True
    assert decision.prev_max_conf == 0.65
    assert decision.prev_max_abs_score == 0.40
    assert "new conf" in decision.reason


# ── rejection cases ──
def test_scale_in_rejected_when_conf_within_margin():
    existing = [_make_order("buy", open_conf=0.65, open_score=0.40)]
    # 0.68 - 0.65 = 0.03 < 0.05
    signal = _make_signal("buy", conf=0.68, combined_score=0.50)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False
    assert "confidence" in decision.reason.lower()


def test_scale_in_rejected_when_score_within_margin():
    existing = [_make_order("buy", open_conf=0.65, open_score=0.40)]
    # 0.42 - 0.40 = 0.02 < 0.05
    signal = _make_signal("buy", conf=0.75, combined_score=0.42)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False
    assert "score" in decision.reason.lower()


def test_scale_in_rejected_when_opposite_direction():
    existing = [_make_order("buy", open_conf=0.65, open_score=0.40)]
    signal = _make_signal("sell", conf=0.85, combined_score=-0.55)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False
    assert "no eligible" in decision.reason.lower()


def test_scale_in_rejected_when_only_legacy_position():
    existing = [_make_order("buy", open_conf=None, open_score=None)]
    signal = _make_signal("buy", conf=0.85, combined_score=0.60)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False
    assert "no eligible" in decision.reason.lower()


def test_scale_in_rejected_when_hold_signal():
    existing = [_make_order("buy", open_conf=0.65, open_score=0.40)]
    signal = _make_signal("hold", conf=0.50, combined_score=0.10)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False
    assert "not directional" in decision.reason.lower()


def test_scale_in_rejected_when_empty_position_list():
    decision = should_scale_in(
        _make_signal("buy", conf=0.80, combined_score=0.50),
        [],
        conf_margin=0.05, score_margin=0.05,
    )
    assert decision.allowed is False


# ── multi-position cases ──
def test_scale_in_compares_only_against_non_legacy():
    existing = [
        _make_order("buy", open_conf=None, open_score=None),    # legacy
        _make_order("buy", open_conf=0.65, open_score=0.40),    # 比較対象
    ]
    signal = _make_signal("buy", conf=0.75, combined_score=0.50)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is True
    assert decision.prev_max_conf == 0.65
    assert decision.prev_max_abs_score == 0.40


def test_scale_in_uses_max_conf_across_multiple_positions_rejected():
    existing = [
        _make_order("buy", open_conf=0.65, open_score=0.30),
        _make_order("buy", open_conf=0.70, open_score=0.50),  # max conf
    ]
    # 0.74 - 0.70 = 0.04 < 0.05
    signal = _make_signal("buy", conf=0.74, combined_score=0.60)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is False


def test_scale_in_uses_max_conf_across_multiple_positions_allowed():
    existing = [
        _make_order("buy", open_conf=0.65, open_score=0.30),
        _make_order("buy", open_conf=0.70, open_score=0.50),
    ]
    signal = _make_signal("buy", conf=0.78, combined_score=0.60)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is True


def test_scale_in_max_conf_and_max_score_can_come_from_different_positions():
    existing = [
        _make_order("buy", open_conf=0.70, open_score=0.30),   # max conf
        _make_order("buy", open_conf=0.60, open_score=0.50),   # max |score|
    ]
    signal = _make_signal("buy", conf=0.76, combined_score=0.56)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is True
    assert decision.prev_max_conf == 0.70
    assert decision.prev_max_abs_score == 0.50


def test_scale_in_negative_score_for_sell_signal():
    existing = [_make_order("sell", open_conf=0.65, open_score=-0.40)]
    signal = _make_signal("sell", conf=0.75, combined_score=-0.50)
    decision = should_scale_in(signal, existing, conf_margin=0.05, score_margin=0.05)
    assert decision.allowed is True
    assert decision.prev_max_abs_score == 0.40


# ── evaluate_pre_execution_checks ──

import pytest

from src.persistence.state_store import StateStore
from src.signals.scale_in import evaluate_pre_execution_checks
from src.trading.position_manager import PositionManager


@pytest.fixture
def position_mgr(tmp_path):
    return PositionManager(StateStore(tmp_path), initial_balance=10000.0, context="Test")


def _common_kwargs(scale_in_enabled: bool = False) -> dict:
    return dict(
        max_positions_per_pair=2,
        scale_in_enabled=scale_in_enabled,
        scale_in_conf_margin=0.05,
        scale_in_score_margin=0.05,
        drawdown_kill_switch_enabled=False,
        drawdown_kill_switch_max_pct=0.10,
        drawdown_kill_switch_lookback_days=0,
    )


def test_pre_exec_returns_allowed_when_no_existing_position(position_mgr):
    signal = _make_signal("buy", conf=0.75, combined_score=0.50)
    status, reason = evaluate_pre_execution_checks(
        signal, position_mgr, **_common_kwargs(),
    )
    assert status == "allowed"


def test_pre_exec_returns_skip_when_max_per_pair_reached(position_mgr):
    position_mgr.open_position(_make_order("buy", open_conf=0.65, open_score=0.40))
    position_mgr.open_position(_make_order("buy", open_conf=0.70, open_score=0.50))
    signal = _make_signal("buy", conf=0.85, combined_score=0.60)
    status, reason = evaluate_pre_execution_checks(
        signal, position_mgr, **_common_kwargs(scale_in_enabled=True),
    )
    assert status == "skip"
    assert "max" in reason.lower() or "positions" in reason.lower()


def test_pre_exec_returns_skip_when_existing_pos_and_scale_in_disabled(position_mgr):
    position_mgr.open_position(_make_order("buy", open_conf=0.65, open_score=0.40))
    signal = _make_signal("buy", conf=0.85, combined_score=0.60)
    status, reason = evaluate_pre_execution_checks(
        signal, position_mgr, **_common_kwargs(scale_in_enabled=False),
    )
    assert status == "skip"
    assert "scale-in disabled" in reason


def test_pre_exec_returns_scale_in_when_judgment_allowed(position_mgr):
    position_mgr.open_position(_make_order("buy", open_conf=0.65, open_score=0.40))
    signal = _make_signal("buy", conf=0.78, combined_score=0.60)
    status, reason = evaluate_pre_execution_checks(
        signal, position_mgr, **_common_kwargs(scale_in_enabled=True),
    )
    assert status == "scale_in"


def test_pre_exec_returns_skip_when_scale_in_judgment_rejects(position_mgr):
    position_mgr.open_position(_make_order("buy", open_conf=0.65, open_score=0.40))
    # margin 不足
    signal = _make_signal("buy", conf=0.68, combined_score=0.42)
    status, reason = evaluate_pre_execution_checks(
        signal, position_mgr, **_common_kwargs(scale_in_enabled=True),
    )
    assert status == "skip"
    assert "scale-in rejected" in reason
