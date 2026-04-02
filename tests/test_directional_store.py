# tests/test_directional_store.py
from __future__ import annotations

import pytest

from src.rag.directional_store import DirectionalStore


@pytest.fixture
def store(tmp_path):
    return DirectionalStore(tmp_path / "test_rag")


def _dummy_embedding(dim: int = 768) -> list[float]:
    return [0.1] * dim


def test_upsert_and_query_bullish(store):
    store.upsert(
        entry_id="sess-001_entry",
        text="EURUSD bullish setup, strong momentum",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-001",
        session_type="trade",
        phase="entry",
        signal_score=0.35,
        confidence=0.78,
    )
    results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
    )
    assert len(results) == 1
    assert results[0]["metadata"]["session_id"] == "sess-001"


def test_upsert_bearish_not_in_bullish(store):
    store.upsert(
        entry_id="sess-002_entry",
        text="EURUSD bearish reversal",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id="sess-002",
        session_type="trade",
        phase="entry",
        signal_score=-0.40,
        confidence=0.80,
    )
    bullish_results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
    )
    assert len(bullish_results) == 0

    bearish_results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bearish",
        top_k=5,
    )
    assert len(bearish_results) == 1


def test_query_complete_only(store):
    store.upsert(
        entry_id="sess-003_entry",
        text="Entry analysis",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-003",
        session_type="trade",
        phase="entry",
        signal_score=0.30,
        confidence=0.70,
    )
    store.upsert(
        entry_id="sess-004_complete",
        text="Complete cycle with result",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-004",
        session_type="trade",
        phase="complete",
        signal_score=0.40,
        confidence=0.80,
        outcome="win",
        realized_pnl=5.0,
    )
    results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
        phase_filter="complete",
    )
    assert len(results) == 1
    assert results[0]["metadata"]["session_id"] == "sess-004"
