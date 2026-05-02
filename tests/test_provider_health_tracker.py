"""ProviderHealthTracker のユニットテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.data.provider_health_tracker import ProviderHealthTracker


def test_first_failure_no_transition():
    tracker = ProviderHealthTracker()
    now = datetime(2026, 4, 29, 12, 0, 0)
    transitioning = tracker.record_failure(now)
    assert transitioning is False
    assert tracker.is_degraded is False


def test_two_failures_within_window_triggers_degraded():
    tracker = ProviderHealthTracker()
    t0 = datetime(2026, 4, 29, 12, 0, 0)
    tracker.record_failure(t0)
    t1 = t0 + timedelta(minutes=4)
    transitioning = tracker.record_failure(t1)
    assert transitioning is True
    assert tracker.is_degraded is True


def test_two_failures_outside_window_no_degraded():
    tracker = ProviderHealthTracker()
    t0 = datetime(2026, 4, 29, 12, 0, 0)
    tracker.record_failure(t0)
    t1 = t0 + timedelta(minutes=6)    # 5分超過
    transitioning = tracker.record_failure(t1)
    assert transitioning is False
    assert tracker.is_degraded is False


def test_success_during_degraded_triggers_recovery():
    tracker = ProviderHealthTracker()
    t0 = datetime(2026, 4, 29, 12, 0, 0)
    tracker.record_failure(t0)
    tracker.record_failure(t0 + timedelta(minutes=2))
    assert tracker.is_degraded
    recovered = tracker.record_success(t0 + timedelta(minutes=10))
    assert recovered is True
    assert tracker.is_degraded is False


def test_success_when_not_degraded_returns_false():
    tracker = ProviderHealthTracker()
    recovered = tracker.record_success(datetime.now())
    assert recovered is False


def test_third_failure_in_degraded_state_no_re_trigger():
    """degraded 中に追加失敗があっても、再度 transitioning=True にはならない。"""
    tracker = ProviderHealthTracker()
    t0 = datetime(2026, 4, 29, 12, 0, 0)
    tracker.record_failure(t0)
    tracker.record_failure(t0 + timedelta(minutes=2))    # degraded
    transitioning = tracker.record_failure(t0 + timedelta(minutes=4))
    assert transitioning is False
