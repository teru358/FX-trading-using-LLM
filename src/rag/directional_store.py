# src/rag/directional_store.py
"""方向別ChromaDBコレクション管理。

bullish/bearish のデータを分離して蓄積・検索する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import chromadb

logger = logging.getLogger(__name__)

_BULLISH_COL = "fx_reflections_bullish"
_BEARISH_COL = "fx_reflections_bearish"


class DirectionalStore:
    """方向別のChromaDBコレクションを管理する。"""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._bullish = self._client.get_or_create_collection(
            name=_BULLISH_COL,
            metadata={"hnsw:space": "cosine"},
        )
        self._bearish = self._client.get_or_create_collection(
            name=_BEARISH_COL,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"DirectionalStore ready (bullish={self._bullish.count()}, "
            f"bearish={self._bearish.count()})"
        )

    def _collection(self, direction: str):
        if direction == "bullish":
            return self._bullish
        elif direction == "bearish":
            return self._bearish
        raise ValueError(f"Invalid direction: {direction}")

    def upsert(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        direction: str,
        pair: str,
        session_id: str,
        session_type: str,
        phase: str,
        signal_score: float,
        confidence: float,
        outcome: str | None = None,
        realized_pnl: float | None = None,
        close_reason: str | None = None,
        horizon: str | None = None,
    ) -> None:
        """方向別コレクションにドキュメントを追加する。"""
        metadata: dict = {
            "pair": pair,
            "session_id": session_id,
            "session_type": session_type,
            "phase": phase,
            "signal_score": signal_score,
            "confidence": confidence,
        }
        if outcome is not None:
            metadata["outcome"] = outcome
        if realized_pnl is not None:
            metadata["realized_pnl"] = realized_pnl
        if close_reason is not None:
            metadata["close_reason"] = close_reason
        # horizon キー無し = legacy swing カード規約 (spec V-1)。
        # None を渡すと ChromaDB がメタデータ値として拒否するため、
        # 値がある場合のみキーを追加する (Falsy な "" も除外)。
        if horizon:
            metadata["horizon"] = horizon

        col = self._collection(direction)
        col.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        logger.debug(f"Upserted {entry_id} to {direction} collection")

    def query(
        self,
        query_embedding: list[float],
        direction: str,
        top_k: int = 5,
        phase_filter: str | None = None,
    ) -> list[dict]:
        """方向別コレクションをベクトル検索する。"""
        col = self._collection(direction)
        if col.count() == 0:
            return []

        where = None
        if phase_filter:
            where = {"phase": {"$eq": phase_filter}}

        try:
            results = col.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, col.count()),
                where=where,
            )
        except Exception:
            return []

        entries = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for i, doc in enumerate(docs):
            entries.append({
                "text": doc,
                "metadata": metas[i],
                "distance": distances[i] if i < len(distances) else None,
            })
        return entries

    def count(self, direction: str) -> int:
        return self._collection(direction).count()
