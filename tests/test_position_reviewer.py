"""position_reviewer.review_open_positions() のテスト。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from src.signals.signal_combiner import TradeSignal
from src.trading.position_reviewer import review_open_positions
from src.utils.clock import db_now


def _make_signal(pair: str, predicted_direction: str,
                 confidence: float, combined_score: float) -> TradeSignal:
    """テスト用 TradeSignal を組み立てるヘルパー。

    NOTE: `predicted_direction` は `combined_score` から自動導出されず手動指定する。
    Layer 1 は `predicted_direction` フィールドを直接参照するため、
    テストが検証したい方向と一致させること。
    """
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis

    news = NewsSentiment(pair=pair, sentiment_score=combined_score, confidence=confidence)
    price = PriceAnalysis(
        pair=pair, direction_bias="long" if combined_score > 0 else "short",
        bias_score=combined_score, confidence=confidence,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="test", analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair,
        action="buy" if combined_score > 0 else "sell",
        predicted_direction=predicted_direction,
        combined_score=combined_score,
        confidence=confidence,
        entry_price=150.0,
        stop_loss=148.0,
        take_profit=153.0,
        position_size=10000.0,
        signal_reason="test",
        detail_reason="test",
        news=news,
        price=price,
        generated_at=datetime.now(),
    )


def test_layer1_reversal_closes(buy_order):
    """Layer1: BUY ポジションに bearish シグナル + confidence ≥ 0.70 → 決済判断が返る。"""
    buy_order.opened_at = db_now() - timedelta(minutes=250)  # NEW: min_holding を超える
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_confidence_min=0.70,
        reversal_score_threshold=0.25,
    )
    assert len(decisions) == 1
    assert decisions[0].close_reason == "reversal"
    assert decisions[0].order_id == buy_order.order_id


def test_layer1_low_score_skips(buy_order):
    """Layer1: confidence ≥ 0.70 でも |score| < reversal_score_threshold → 決済しない。"""
    buy_order.opened_at = db_now() - timedelta(minutes=250)  # NEW: min_holding 超
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.10)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_confidence_min=0.70,
        reversal_score_threshold=0.25,
    )
    assert len(decisions) == 0


def test_layer1_low_confidence_skips(buy_order):
    """Layer1: confidence < 0.70 → 決済判断は返らない。"""
    buy_order.opened_at = db_now() - timedelta(minutes=250)  # NEW: min_holding 超
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.60, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_confidence_min=0.70,
    )
    assert len(decisions) == 0
# --- NEW TESTS ---


def test_layer1_reversal_blocked_by_min_holding(buy_order):
    """holding < 240min かつ反転シグナルでも reversal decision を返さない。"""
    buy_order.opened_at = db_now() - timedelta(minutes=60)  # 1h < 240min
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_min_holding_minutes=240,
    )
    assert decisions == []


def test_layer1_reversal_fires_after_min_holding(buy_order):
    """holding >= 240min なら反転発火。"""
    buy_order.opened_at = db_now() - timedelta(minutes=250)
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_min_holding_minutes=240,
    )
    assert len(decisions) == 1
    assert decisions[0].close_reason == "reversal"


def test_diagnostic_log_emits_layer_reasons_when_no_action(buy_order, caplog):
    """発火しない position に DEBUG ログが出る (min_holding 未達)。l1=min_holding_not_met。"""
    caplog.set_level(logging.DEBUG, logger="src.trading.position_reviewer")
    buy_order.opened_at = db_now() - timedelta(minutes=120)  # 反転だが min_holding 未達
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_min_holding_minutes=240,
    )
    log_text = "\n".join(r.message for r in caplog.records)
    assert "[REVIEW] USDJPY=X eval:" in log_text
    assert "l1=min_holding_not_met" in log_text


def test_diagnostic_log_l1_no_signal_when_signal_absent(buy_order, caplog):
    """signal が無い場合は l1=no_signal / l3=no_signal。"""
    caplog.set_level(logging.DEBUG, logger="src.trading.position_reviewer")
    review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.5},
    )
    log_text = "\n".join(r.message for r in caplog.records)
    assert "l1=no_signal" in log_text
    assert "time_stop=disabled" in log_text


def test_timeout_only_skips_layer1_and_3(buy_order):
    """timeout_only=True で反転シグナルでも decision を返さない。"""
    buy_order.opened_at = db_now() - timedelta(minutes=300)  # holding 十分
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        timeout_only=True,
    )
    assert decisions == []
