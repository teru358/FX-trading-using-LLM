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
