"""複合 filter と退役カード削除のテスト (spec §3.4b)。"""
import pytest

from src.rag.directional_store import DirectionalStore


@pytest.fixture
def store(tmp_path):
    return DirectionalStore(tmp_path / "chroma")


def _put(store, entry_id, session_type, phase, direction="bullish"):
    store.upsert(
        entry_id=entry_id, text=f"card {entry_id}", embedding=[0.1] * 8,
        direction=direction, pair="USDJPY=X", session_id=entry_id,
        session_type=session_type, phase=phase,
        signal_score=0.0, confidence=0.0,
    )


def _put_legacy(store, entry_id, direction="bullish"):
    """session_type メタデータ導入前のカードを直接投入する (upsert は必須引数)。"""
    col = store._collection(direction)
    col.upsert(
        ids=[entry_id], embeddings=[[0.1] * 8], documents=[f"card {entry_id}"],
        metadatas=[{
            "pair": "USDJPY=X", "session_id": entry_id, "phase": "complete",
            "signal_score": 0.0, "confidence": 0.0,
        }],
    )


@pytest.fixture
def seeded(store):
    _put(store, "t-complete", "trade", "complete")
    _put(store, "t-entry", "trade", "entry")
    _put(store, "f-entry", "forecast", "entry")
    _put(store, "f-complete", "forecast", "complete")
    _put(store, "h-review", "hold", "complete")
    # bearish 側にも退役カードを 1 枚置き、両 collection をループしていることを検証する
    _put(store, "b-f-complete", "forecast", "complete", direction="bearish")
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
    # bullish: t-entry, f-entry, f-complete, h-review / bearish: b-f-complete
    assert deleted == {"bullish": 4, "bearish": 1}
    hits = seeded.query([0.1] * 8, "bullish", top_k=10)
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}
    assert seeded.count("bearish") == 0


def test_cleanup_idempotent(seeded):
    seeded.delete_retired_cards()
    deleted_again = seeded.delete_retired_cards()
    assert deleted_again == {"bullish": 0, "bearish": 0}


def test_trade_complete_survives_cleanup(seeded):
    seeded.delete_retired_cards()
    assert seeded.count("bullish") == 1


def test_cleanup_on_empty_store(store):
    assert store.delete_retired_cards() == {"bullish": 0, "bearish": 0}


def test_cleanup_count_unaffected_by_concurrent_upsert(seeded, monkeypatch):
    """削除対象 ID 確定後に別ジョブが upsert しても件数を誤らない (count 差分方式の回帰)。

    count 差分実装では新規 upsert が削除件数を打ち消し、過小・負値になりうる。
    """
    col = seeded._collection("bullish")
    original_delete = col.delete

    def delete_then_concurrent_upsert(*args, **kwargs):
        result = original_delete(*args, **kwargs)
        # 別ジョブによる新規カード投入を模擬する
        _put(seeded, "concurrent-1", "trade", "complete")
        _put(seeded, "concurrent-2", "trade", "complete")
        return result

    monkeypatch.setattr(col, "delete", delete_then_concurrent_upsert)
    deleted = seeded.delete_retired_cards()
    assert deleted["bullish"] == 4


def test_unknown_session_type_neither_deleted_nor_searched(store):
    """未知/legacy の session_type は削除も検索もされない (意図的な許容、docstring 記載済)。"""
    _put(store, "r-1", "reflection", "complete")
    _put_legacy(store, "legacy-1")
    _put(store, "t-complete", "trade", "complete")

    deleted = store.delete_retired_cards()
    assert deleted["bullish"] == 0
    assert store.count("bullish") == 3

    hits = store.query([0.1] * 8, "bullish", top_k=10,
                       phase_filter="complete", session_type_filter="trade")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}


def test_filter_returns_full_top_k_despite_many_retired_cards(store):
    """n_results は filter 前の全体件数ベースだが、filter 後の上位を正しく返す (LOW-3)。"""
    for i in range(5):
        _put(store, f"tc-{i}", "trade", "complete")
    for i in range(50):
        _put(store, f"ret-{i}", "forecast", "entry")

    hits = store.query([0.1] * 8, "bullish", top_k=3,
                       phase_filter="complete", session_type_filter="trade")
    assert len(hits) == 3
    assert all(h["metadata"]["session_id"].startswith("tc-") for h in hits)

    hits_all = store.query([0.1] * 8, "bullish", top_k=10,
                           phase_filter="complete", session_type_filter="trade")
    assert len(hits_all) == 5
