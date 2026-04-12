"""CircuitBreaker のテスト。"""
from __future__ import annotations

import time

from src.llm.circuit_breaker import CircuitBreaker


def test_initial_state_is_closed():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    assert cb.state == "CLOSED"
    assert cb.allow_request() is True


def test_opens_after_threshold_failures():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "CLOSED"
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.allow_request() is False


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == "CLOSED"


def test_half_open_after_cooldown():
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
    cb.record_failure()
    cb.record_failure()
    # cooldown=0 → 即座に HALF_OPEN
    assert cb.state == "HALF_OPEN"
    assert cb.allow_request() is True


def test_success_after_half_open_closes():
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "HALF_OPEN"
    cb.record_success()
    assert cb.state == "CLOSED"
