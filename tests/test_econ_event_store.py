"""EconEventStore テスト。"""
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.economic_calendar import EconEvent
from src.data.econ_event_store import EconEventStore


def _make_event(event_id: str, minutes_ago: int, importance: int = 1, actual=None) -> EconEvent:
    return EconEvent(
        event_id=event_id,
        title=f"Event {event_id}",
        country="US",
        currency="USD",
        importance=importance,
        event_time=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        actual=actual,
        forecast=3.0,
        previous=2.9,
        unit="%",
    )


def test_upsert_and_get(tmp_path):
    store = EconEventStore(tmp_path / "test.db")
    events = [_make_event("1", 10), _make_event("2", 5)]
    count = store.upsert_events(events)
    assert count == 2

    # idempotent
    count2 = store.upsert_events(events)
    assert count2 == 2  # updated not added


def test_get_unanalyzed_with_actual(tmp_path):
    store = EconEventStore(tmp_path / "test.db")
    # actualあり・未分析
    ev1 = _make_event("1", 10, importance=1, actual=3.5)
    # actualなし（未発表）
    ev2 = _make_event("2", 5, importance=1, actual=None)
    # actualあり・importance不足
    ev3 = _make_event("3", 5, importance=0, actual=2.0)
    store.upsert_events([ev1, ev2, ev3])

    results = store.get_unanalyzed_with_actual(lookback_min=60, min_importance=1)
    ids = [e.event_id for e in results]
    assert "1" in ids
    assert "2" not in ids  # actualなし
    assert "3" not in ids  # importance不足


def test_mark_analyzed(tmp_path):
    store = EconEventStore(tmp_path / "test.db")
    ev = _make_event("1", 10, importance=1, actual=3.5)
    store.upsert_events([ev])
    assert len(store.get_unanalyzed_with_actual(60, 1)) == 1
    store.mark_analyzed("1")
    assert len(store.get_unanalyzed_with_actual(60, 1)) == 0


def test_get_recent_published(tmp_path):
    store = EconEventStore(tmp_path / "test.db")
    # 発表済み (30分前)
    ev1 = _make_event("past", 30, actual=3.0)
    # 未発表 (-10 = 10分後)
    ev2 = _make_event("future", -10, actual=None)
    store.upsert_events([ev1, ev2])

    results = store.get_recent_published(lookback_min=60)
    ids = [e.event_id for e in results]
    assert "past" in ids
    assert "future" not in ids


def test_update_actuals(tmp_path):
    store = EconEventStore(tmp_path / "test.db")
    ev = _make_event("1", 10, importance=1, actual=None)
    store.upsert_events([ev])

    updated = store.update_actuals({"1": {"actual": 3.5}})
    assert updated == 1

    results = store.get_unanalyzed_with_actual(60, 1)
    assert len(results) == 1
    assert results[0].actual == 3.5
