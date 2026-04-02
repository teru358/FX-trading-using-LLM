# tests/test_integration_directional.py
"""方向別RAGの統合テスト: SessionStore + DirectionalStore + RAG補正の連携確認。"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.data.session_store import SessionStore
from src.rag.directional_store import DirectionalStore
from src.signals.rag_adjustment import compute_rag_adjustment, RagAdjustmentConfig


def _dummy_embedding(dim: int = 768) -> list[float]:
    return [0.1] * dim


@pytest.fixture
def session_store(tmp_path):
    return SessionStore(tmp_path / "test.db")


@pytest.fixture
def directional_store(tmp_path):
    return DirectionalStore(tmp_path / "test_rag")


def test_full_trade_lifecycle(session_store, directional_store):
    """発注 → セッション作成 → RAG entry → クローズ → RAG complete → 補正参照。"""
    sid = "integration-001"

    # 1. セッション作成
    session_store.create_session(
        session_id=sid,
        pair="EURUSD=X",
        direction="bearish",
        entry_price=1.15,
        stop_loss=1.16,
        take_profit=1.13,
        position_size=1000.0,
        signal_score=-0.35,
        signal_confidence=0.78,
        macro_context="DXY strong",
        analysis_summary="Strong bearish signal",
        opened_at=datetime(2026, 4, 1, 9, 30),
    )

    # 2. RAG entry注入
    directional_store.upsert(
        entry_id=f"{sid}_entry",
        text="EURUSD bearish strong momentum DXY",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id=sid,
        session_type="trade",
        phase="entry",
        signal_score=-0.35,
        confidence=0.78,
    )

    # 3. クローズ
    session_store.close_session(
        session_id=sid,
        closed_at=datetime(2026, 4, 2, 10, 0),
        close_price=1.13,
        close_reason="take_profit",
        realized_pnl=20.0,
        reflection_text="Trade followed through as expected",
    )

    # 4. RAG complete注入
    directional_store.upsert(
        entry_id=f"{sid}_complete",
        text="EURUSD bearish win take_profit pnl=+20.0",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id=sid,
        session_type="trade",
        phase="complete",
        signal_score=-0.35,
        confidence=0.78,
        outcome="win",
        realized_pnl=20.0,
        close_reason="take_profit",
    )

    # 5. 検証
    session = session_store.get_session(sid)
    assert session.outcome == "win"

    hits = directional_store.query(_dummy_embedding(), "bearish", top_k=5, phase_filter="complete")
    assert len(hits) == 1
    assert hits[0]["metadata"]["outcome"] == "win"


def test_rag_adjustment_with_real_data(directional_store):
    """DirectionalStoreのデータを使って補正値が算出できることを確認。"""
    # bearish wins を蓄積
    for i in range(3):
        directional_store.upsert(
            entry_id=f"bear-{i}_complete",
            text=f"EURUSD bearish win trade {i}",
            embedding=_dummy_embedding(),
            direction="bearish",
            pair="EURUSD=X",
            session_id=f"bear-{i}",
            session_type="trade",
            phase="complete",
            signal_score=-0.30,
            confidence=0.75,
            outcome="win",
            realized_pnl=10.0,
        )

    # bearishシグナルで補正を計算
    same_hits = directional_store.query(_dummy_embedding(), "bearish", top_k=5, phase_filter="complete")
    opposite_hits = directional_store.query(_dummy_embedding(), "bullish", top_k=5, phase_filter="complete")

    cfg = RagAdjustmentConfig()
    adj = compute_rag_adjustment(
        combined_score=-0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    # bearish方向のwin rateが高い → bearish強化（負方向の補正）
    assert adj < 0
