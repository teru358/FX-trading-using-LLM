"""取引サイクル集約通知 (notifier.py) のテスト。"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.config.schema import NotifierConfig
from src.notifications.notifier import CycleSummaryEvent, SignalOutcome


def test_signal_outcome_defaults():
    o = SignalOutcome(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.75, combined_score=0.32,
        reason="r", detail_reason="d",
        news_score=0.12, tech_score=0.37,
    )
    assert o.tv_recommendation == ""
    assert o.rag_note == ""
    assert o.order is None


def test_cycle_summary_event_defaults():
    ev = CycleSummaryEvent(cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[])
    assert ev.halted is False
    assert ev.data_health == []
    assert ev.source == "trading"


def test_notifier_config_has_cycle_summary_flag():
    assert NotifierConfig().notify_on_cycle_summary is True


from src.notifications.notifier import _format_signal_block  # noqa: E402
from src.trading.position_manager import Order  # noqa: E402


def _executed_outcome(**kw) -> SignalOutcome:
    order = Order.new("USDJPY=X", "buy", 159.004, 158.216, 160.580, 1000.0)
    defaults = dict(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.75, combined_score=0.320,
        reason="rates higher + tech long alignment", detail_reason="",
        news_score=0.12, tech_score=0.37, tv_recommendation="BUY", order=order,
    )
    defaults.update(kw)
    return SignalOutcome(**defaults)


def _hold_outcome(**kw) -> SignalOutcome:
    defaults = dict(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.30, combined_score=-0.023,
        reason="confidence too low, NEWS/PRICE conflict", detail_reason="",
        news_score=0.09, tech_score=-0.05, tv_recommendation="STRONG_SELL",
    )
    defaults.update(kw)
    return SignalOutcome(**defaults)


def test_format_signal_block_executed_has_all_lines():
    block = _format_signal_block(_executed_outcome())
    assert "📈 USDJPY=X BUY EXECUTED" in block
    assert "score +0.320 | conf 75% | RR 2.00" in block
    assert "entry 159.00400" in block
    assert "SL 158.21600" in block
    assert "drivers: News +0.12 / Tech +0.37 / TV BUY" in block
    assert "reason: rates higher" in block


def test_format_signal_block_hold_omits_entry_sl_tp_and_rr():
    block = _format_signal_block(_hold_outcome())
    assert "⏸ EURUSD=X HOLD" in block
    assert "score -0.023 | conf 30%" in block
    assert "entry" not in block
    assert "RR" not in block
    assert "drivers: News +0.09 / Tech -0.05 / TV STRONG_SELL" in block
    assert "reason: confidence too low" in block


def test_format_signal_block_rejected_shows_reason_not_existing_position():
    o = _hold_outcome(
        pair="EURUSD=X", action="sell", status="rejected",
        reason="発注拒否 (broker): retcode=10016 Invalid stops",
    )
    block = _format_signal_block(o)
    assert "🚫 EURUSD=X SELL REJECTED" in block
    assert "retcode=10016" in block
    assert "既存ポジション" not in block


def test_format_signal_block_failed():
    block = _format_signal_block(
        _hold_outcome(pair="USDJPY=X", action="buy", status="failed",
                      reason="bridge unreachable"))
    assert "❌ USDJPY=X BUY FAILED" in block
    assert "reason: bridge unreachable" in block


def test_format_signal_block_skipped_atr_reason():
    block = _format_signal_block(
        _hold_outcome(pair="USDJPY=X", action="buy", status="skipped",
                      reason="ATR SL/TP calculation failed"))
    assert "⏭ USDJPY=X BUY SKIPPED" in block
    assert "reason: ATR SL/TP calculation failed" in block


def test_format_signal_block_omits_tv_when_empty():
    block = _format_signal_block(_hold_outcome(tv_recommendation=""))
    assert "TV" not in block
    assert "drivers: News +0.09 / Tech -0.05" in block


def test_format_signal_block_shows_rag_note():
    block = _format_signal_block(
        _hold_outcome(rag_note="score -0.023→+0.115, hold→buy"))
    assert "RAG: score -0.023→+0.115, hold→buy" in block


def test_format_signal_block_no_rag_line_when_empty():
    assert "RAG:" not in _format_signal_block(_hold_outcome())


def test_format_signal_block_scale_in_label():
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0, is_scale_in=True)
    block = _format_signal_block(_executed_outcome(order=order))
    assert "(scale-in)" in block


from src.notifications.notifier import (  # noqa: E402
    NotifierAdapter,
    _format_cycle_summary,
)


class _CapturingNotifier(NotifierAdapter):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def test_format_cycle_summary_header_and_counts():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30),
        outcomes=[_executed_outcome(), _hold_outcome()],
    )
    msg = _format_cycle_summary(event)
    assert msg.startswith("🟢 取引サイクル 17:30 JST")
    assert "結果: 1発注 / 1HOLD / 0拒否 / 0失敗" in msg
    assert "📈 USDJPY=X BUY EXECUTED" in msg
    assert "⏸ EURUSD=X HOLD" in msg


def test_format_cycle_summary_warning_emoji_on_rejection():
    o_rej = _hold_outcome(action="sell", status="rejected", reason="retcode=10016")
    event = CycleSummaryEvent(cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[o_rej])
    msg = _format_cycle_summary(event)
    assert msg.startswith("⚠️")
    assert "0発注 / 0HOLD / 1拒否 / 0失敗" in msg


def test_format_cycle_summary_skip_count_only_when_positive():
    no_skip = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    assert "スキップ" not in _format_cycle_summary(no_skip)
    o_skip = _hold_outcome(action="buy", status="skipped", reason="既存ポジションあり")
    with_skip = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[o_skip])
    assert "1スキップ" in _format_cycle_summary(with_skip)


def test_format_cycle_summary_data_health_line():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()],
        data_health=["EURUSD=X 分析失敗"],
    )
    msg = _format_cycle_summary(event)
    assert "⚠ Data: EURUSD=X 分析失敗" in msg
    assert msg.startswith("⚠️")


def test_format_cycle_summary_no_data_line_when_healthy():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    assert "Data:" not in _format_cycle_summary(event)


def test_format_cycle_summary_halt():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[], halted=True)
    msg = _format_cycle_summary(event)
    assert msg.startswith("🛑 取引サイクル 17:30 JST")
    assert "halt 中" in msg
    assert "新規発注分析をスキップ" in msg


@pytest.mark.asyncio
async def test_notify_cycle_summary_calls_send():
    notifier = _CapturingNotifier()
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    await notifier.notify_cycle_summary(event)
    assert len(notifier.messages) == 1
    assert "取引サイクル 17:30 JST" in notifier.messages[0]
