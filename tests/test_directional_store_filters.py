"""複合 filter と退役カード削除のテスト (spec §3.4b)。"""
import pytest

from src.rag.directional_store import DirectionalStore


@pytest.fixture
def store(tmp_path):
    return DirectionalStore(tmp_path / "chroma")


def _put(store, entry_id, session_type, phase):
    store.upsert(
        entry_id=entry_id, text=f"card {entry_id}", embedding=[0.1] * 8,
        direction="bullish", pair="USDJPY=X", session_id=entry_id,
        session_type=session_type, phase=phase,
        signal_score=0.0, confidence=0.0,
    )


@pytest.fixture
def seeded(store):
    _put(store, "t-complete", "trade", "complete")
    _put(store, "t-entry", "trade", "entry")
    _put(store, "f-entry", "forecast", "entry")
    _put(store, "f-complete", "forecast", "complete")
    _put(store, "h-review", "hold", "complete")
    return store


def test_combined_filter_returns_only_trade_complete(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10,
                        phase_filter="complete", session_type_filter="trade")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}


def test_session_type_only_filter(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10, session_type_filter="trade")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete", "t-entry"}


def test_phase_only_filter_unchanged(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10, phase_filter="complete")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete", "f-complete", "h-review"}


def test_no_filter_returns_all(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10)
    assert len(hits) == 5


def test_cleanup_deletes_only_retired_cards(seeded):
    deleted = seeded.delete_retired_cards()
    assert deleted["bullish"] == 4       # t-entry, f-entry, f-complete, h-review
    hits = seeded.query([0.1] * 8, "bullish", top_k=10)
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}


def test_cleanup_idempotent(seeded):
    seeded.delete_retired_cards()
    deleted_again = seeded.delete_retired_cards()
    assert deleted_again == {"bullish": 0, "bearish": 0}


def test_trade_complete_survives_cleanup(seeded):
    seeded.delete_retired_cards()
    assert seeded.count("bullish") == 1


def test_cleanup_on_empty_store(store):
    assert store.delete_retired_cards() == {"bullish": 0, "bearish": 0}
