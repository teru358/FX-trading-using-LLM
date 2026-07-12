"""Tests for ask insights RAG feature."""
from __future__ import annotations

from datetime import datetime


def test_vector_store_insight_upsert(tmp_path):
    """VectorStore.upsert_insight が正しく動作すること"""
    from src.rag.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "test_insights_db")
    store.upsert_insight(
        entry_id="test_1",
        text="テスト洞察",
        embedding=[0.1] * 768,
        pair="USDJPY=X",
        insight_type="analysis",
        source_question="ドル円の分析をして",
        created_at=datetime.now(),
    )
    results = store.get_recent_insights(pair="USDJPY=X", limit=5)
    assert len(results) == 1
    assert results[0]["text"] == "テスト洞察"
    assert results[0]["metadata"]["pair"] == "USDJPY=X"


def test_vector_store_query_insights(tmp_path):
    """query_insights がセマンティック検索できること"""
    from src.rag.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "test_insights_query_db")
    store.upsert_insight(
        entry_id="test_q1",
        text="ドル円は強い上昇トレンド",
        embedding=[0.1] * 768,
        pair="USDJPY=X",
        insight_type="analysis",
        source_question="ドル円どう？",
        created_at=datetime.now(),
    )
    results = store.query_insights(
        query_embedding=[0.1] * 768,
        top_k=3,
    )
    assert len(results) >= 1
    assert "上昇トレンド" in results[0]["text"]
