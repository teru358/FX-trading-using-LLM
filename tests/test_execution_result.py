"""ExecutionResult 分類と、それを反映する skip 通知のテスト。

バグ2: 発注失敗が理由を問わず「既存ポジションのためスキップ」と通知される問題。
execute_signal の戻り値を ExecutionResult (executed/skipped/halted/rejected/failed)
に分類し、通知文面を outcome 別に出し分けることを検証する。
"""
from __future__ import annotations

import asyncio

from src.notifications.notifier import NotifierAdapter, SignalSkippedEvent
from src.trading.broker_adapter import ExecutionResult


class _CapturingNotifier(NotifierAdapter):
    """send() された文字列を貯めるだけのテスト用 notifier。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _notify(event: SignalSkippedEvent) -> str:
    n = _CapturingNotifier()
    asyncio.run(n.notify_signal_skipped(event))
    return n.messages[0]


# ── ExecutionResult 型 ──────────────────────────────────────────

def test_executed_result_is_executed_and_carries_order():
    sentinel = object()
    r = ExecutionResult.executed(sentinel)
    assert r.is_executed is True
    assert r.outcome == "executed"
    assert r.order is sentinel


def test_rejected_result_is_not_executed():
    r = ExecutionResult.rejected("retcode=10016 Invalid stops")
    assert r.is_executed is False
    assert r.outcome == "rejected"
    assert r.order is None
    assert "10016" in r.reason


def test_skipped_halted_failed_factories():
    assert ExecutionResult.skipped("x").outcome == "skipped"
    assert ExecutionResult.halted("x").outcome == "halted"
    assert ExecutionResult.failed("x").outcome == "failed"


# ── 通知: outcome 別の文面 ──────────────────────────────────────

def test_rejected_outcome_is_not_labeled_existing_position():
    """発注拒否を「既存ポジションのためスキップ」と表示しない (バグ2の核心)。"""
    msg = _notify(SignalSkippedEvent(
        pair="EURUSD=X", action="sell", confidence=0.71,
        signal_reason="news/tech short",
        outcome="rejected",
        skip_reason="発注拒否: retcode=10016 Invalid stops",
        source="trading",
    ))
    assert "既存ポジション" not in msg
    assert "発注拒否" in msg
    assert "retcode=10016" in msg


def test_failed_outcome_is_not_labeled_existing_position():
    msg = _notify(SignalSkippedEvent(
        pair="USDJPY=X", action="buy", confidence=0.68,
        signal_reason="news/tech long",
        outcome="failed",
        skip_reason="bridge HTTP 500: Invalid comment",
        source="trading",
    ))
    assert "既存ポジション" not in msg
    assert "発注失敗" in msg


def test_skipped_outcome_shows_actual_reason():
    msg = _notify(SignalSkippedEvent(
        pair="EURUSD=X", action="sell", confidence=0.71,
        signal_reason="news/tech short",
        outcome="skipped",
        skip_reason="EURUSD=X already has open position (scale-in disabled)",
        source="trading",
    ))
    assert "EURUSD=X already has open position" in msg


def test_halted_outcome_mentions_halt():
    msg = _notify(SignalSkippedEvent(
        pair="USDJPY=X", action="buy", confidence=0.68,
        signal_reason="news/tech long",
        outcome="halted",
        skip_reason="finance soft-halt 中",
        source="trading",
    ))
    assert "halt" in msg.lower()
    assert "既存ポジション" not in msg


def test_hold_action_still_renders_hold_message():
    """hold は従来どおり HOLD 表示 (outcome に左右されない)。"""
    msg = _notify(SignalSkippedEvent(
        pair="USDJPY=X", action="hold", confidence=0.61,
        signal_reason="weak signal",
        predicted_direction="bullish",
        outcome="skipped",
        source="trading",
    ))
    assert "HOLD" in msg
