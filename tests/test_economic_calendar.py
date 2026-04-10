"""Economic Calendar API テスト。"""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.economic_calendar import (
    EconEvent, classify_surprise, fetch_events, parse_event
)


def test_parse_event_basic():
    raw = {
        "id": "419568",
        "title": "Core CPI (YoY)",
        "country": "US",
        "currency": "USD",
        "importance": 1,
        "actual": 3.2,
        "previous": 3.1,
        "forecast": 3.0,
        "unit": "%",
        "date": "2026-04-10T12:30:00.000Z",
    }
    ev = parse_event(raw)
    assert ev.event_id == "419568"
    assert ev.title == "Core CPI (YoY)"
    assert ev.currency == "USD"
    assert ev.importance == 1
    assert ev.actual == 3.2
    assert ev.forecast == 3.0


def test_parse_event_missing_actual():
    raw = {
        "id": "1",
        "title": "Test",
        "country": "US",
        "currency": "USD",
        "importance": 0,
        "actual": None,
        "forecast": 1.5,
        "previous": 1.3,
        "date": "2026-04-10T12:30:00.000Z",
    }
    ev = parse_event(raw)
    assert ev.actual is None


def test_classify_surprise_strong_beat():
    assert classify_surprise(actual=3.5, forecast=3.0) == "strong_beat"


def test_classify_surprise_beat():
    assert classify_surprise(actual=3.1, forecast=3.0) == "beat"


def test_classify_surprise_miss():
    assert classify_surprise(actual=2.9, forecast=3.0) == "miss"


def test_classify_surprise_strong_miss():
    assert classify_surprise(actual=2.5, forecast=3.0) == "strong_miss"


def test_classify_surprise_none_values():
    assert classify_surprise(actual=None, forecast=3.0) is None
    assert classify_surprise(actual=3.0, forecast=None) is None
    assert classify_surprise(actual=3.0, forecast=0) is None


def test_fetch_events_calls_api():
    mock_response = {
        "status": "ok",
        "result": [
            {
                "id": "1", "title": "CPI", "country": "US", "currency": "USD",
                "importance": 1, "actual": None, "forecast": 3.0, "previous": 2.9,
                "unit": "%", "date": "2026-04-10T12:30:00.000Z",
            }
        ]
    }
    with patch("httpx.Client") as mock_client:
        ctx = mock_client.return_value.__enter__.return_value
        resp = MagicMock()
        resp.json.return_value = mock_response
        resp.raise_for_status = MagicMock()
        ctx.get.return_value = resp

        from_utc = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        to_utc = datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc)
        events = fetch_events(from_utc, to_utc, ["US"])
    assert len(events) == 1
    assert events[0].event_id == "1"
