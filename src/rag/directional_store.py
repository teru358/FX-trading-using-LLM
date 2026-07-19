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

# 退役カード (forecast/hold + trade entry) の where 条件 (spec §3.4b)。
# migration スクリプトの dry-run 件数カウントと共有し、条件のドリフトを防ぐ。
RETIRED_CARDS_WHERE = {"$or": [
    {"session_type": {"$in": ["forecast", "hold"]}},
    {"$and": [
        {"session_type": {"$eq": "trade"}},
        {"phase": {"$eq": "entry"}},
    ]},
]}


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
        session_type_filter: str | None = None,
    ) -> list[dict]:
        """方向別コレクションをベクトル検索する。"""
        col = self._collection(direction)
        if col.count() == 0:
            return []

        clauses: list[dict] = []
        if phase_filter:
            clauses.append({"phase": {"$eq": phase_filter}})
        if session_type_filter:
            clauses.append({"session_type": {"$eq": session_type_filter}})
        if len(clauses) == 1:
            where = clauses[0]
        elif clauses:
            where = {"$and": clauses}
        else:
            where = None

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

    def delete_retired_cards(self) -> dict[str, int]:
        """forecast/hold カードと trade entry カードを削除する (冪等、spec §3.4b)。

        forecast サイクル・取引サイクル退役に伴い、これらの生産者は全て廃止される。
        既存の蓄積カードが教訓として検索され続けないよう掃除する。

        削除対象は以下を満たすカード:
          - session_type が "forecast" または "hold"
          - session_type == "trade" かつ phase == "entry"

        Returns:
            方向 ("bullish"/"bearish") ごとの削除件数。件数は削除対象 ID を
            事前に確定して数えるため、並行 upsert が走っていても正確。

        Note:
            session_type メタデータ導入前の legacy カード (キー自体が無い) は
            $in にも $eq にもマッチしないため削除されない。これは spec §3.4b が
            削除対象を session_type で定義しているための意図的な許容である。
            同カードは news_collector の複合 filter からも除外されるため、
            「残存するが検索されない」死蔵データとなる。結果として本メソッドの
            報告件数と実 DB 残存件数は一致しないことがある。

        Raises:
            Exception: ChromaDB の get/delete が失敗した場合、例外はそのまま
                伝搬する (migration スクリプトが失敗を検知できるようにするため)。
                bullish の削除成功後に bearish で例外が出ると片側だけ掃除された
                状態で落ちるが、本メソッドは冪等なので再実行で回復できる。
        """
        counts: dict[str, int] = {}
        for direction in ("bullish", "bearish"):
            col = self._collection(direction)
            # 削除は 1 回の $or にまとめる (中間状態を作らない)。件数は
            # count 差分ではなく対象 ID 数で確定する (並行 upsert に非依存)。
            retired = col.get(where=RETIRED_CARDS_WHERE, include=[])
            ids = retired["ids"]
            if ids:
                col.delete(ids=ids)
                logger.info(
                    f"Deleted {len(ids)} retired cards from {direction} collection"
                )
            counts[direction] = len(ids)
        return counts

    def count(self, direction: str) -> int:
        return self._collection(direction).count()
