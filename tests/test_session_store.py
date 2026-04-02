# tests/test_session_store.py
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.data.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "test.db")


def test_create_and_get_session(store):
    store.create_session(
        session_id="sess-001",
        pair="EURUSD=X",
        direction="bearish",
        entry_price=1.15,
        stop_loss=1.16,
        take_profit=1.13,
        position_size=1000.0,
        signal_score=-0.35,
        signal_confidence=0.78,
        macro_context="DXY long",
        analysis_summary="Strong bearish signal",
        opened_at=datetime(2026, 4, 1, 9, 30),
    )
    session = store.get_session("sess-001")
    assert session is not None
    assert session.pair == "EURUSD=X"
    assert session.direction == "bearish"
    assert session.outcome is None


def test_close_session(store):
    store.create_session(
        session_id="sess-002",
        pair="USDJPY=X",
        direction="bullish",
        entry_price=150.0,
        stop_loss=149.0,
        take_profit=152.0,
        position_size=1000.0,
        signal_score=0.40,
        signal_confidence=0.80,
        macro_context="",
        analysis_summary="Bullish setup",
        opened_at=datetime(2026, 4, 1, 15, 0),
    )
    store.close_session(
        session_id="sess-002",
        closed_at=datetime(2026, 4, 2, 10, 0),
        close_price=151.5,
        close_reason="take_profit",
        realized_pnl=1500.0,
        reflection_text="Good trade, trend followed through",
    )
    session = store.get_session("sess-002")
    assert session.outcome == "win"
    assert session.realized_pnl == 1500.0
    assert session.reflection_text == "Good trade, trend followed through"


def test_get_nonexistent_session(store):
    assert store.get_session("nonexistent") is None


def test_close_session_loss(store):
    store.create_session(
        session_id="sess-003",
        pair="EURUSD=X",
        direction="bullish",
        entry_price=1.15,
        stop_loss=1.14,
        take_profit=1.17,
        position_size=1000.0,
        signal_score=0.30,
        signal_confidence=0.70,
        macro_context="",
        analysis_summary="Weak bullish",
        opened_at=datetime(2026, 4, 1, 9, 0),
    )
    store.close_session(
        session_id="sess-003",
        closed_at=datetime(2026, 4, 1, 12, 0),
        close_price=1.14,
        close_reason="stop_loss",
        realized_pnl=-10.0,
        reflection_text="",
    )
    session = store.get_session("sess-003")
    assert session.outcome == "loss"
