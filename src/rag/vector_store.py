from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import chromadb

logger = logging.getLogger(__name__)

# ChromaDB コレクション名
_NEWS_COL = "fx_news"
_REFLECTION_COL = "fx_reflections"


class VectorStore:
    """ChromaDB によるローカルベクトルストア。

    ニュース分析結果と振り返りサマリーの2コレクションを管理する。
    """

    def __init__(self, db_path: Path) -> None:
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._news = self._client.get_or_create_collection(
            name=_NEWS_COL,
            metadata={"hnsw:space": "cosine"},
        )
        self._reflections = self._client.get_or_create_collection(
            name=_REFLECTION_COL,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"VectorStore ready at {db_path} "
            f"(news={self._news.count()}, reflections={self._reflections.count()})"
        )

    # ---- Category News (カテゴリ別分析結果) --------------------------------

    def upsert_category_news(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        category: str,
        sentiment_score: float,
        confidence: float,
        key_themes: list[str],
        summary: str,
        news_count: int,
        collected_at: datetime,
    ) -> None:
        """カテゴリ別ニュース分析結果を格納する。"""
        self._news.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "category": category,
                "sentiment_score": sentiment_score,
                "confidence": confidence,
                "key_themes": ", ".join(key_themes),
                "summary": summary,
                "news_count": news_count,
                "collected_at": collected_at.isoformat(),
                "collected_ts": collected_at.timestamp(),
            }],
        )
        logger.debug(f"Upserted category news {entry_id} ({category})")

    def get_last_collected_ts(self, category: str) -> float | None:
        """カテゴリの最終収集タイムスタンプを返す。"""
        try:
            results = self._news.get(
                where={"category": {"$eq": category}},
                include=["metadatas"],
            )
        except Exception:
            return None
        timestamps = [m.get("collected_ts", 0) for m in results.get("metadatas", [])]
        return max(timestamps) if timestamps else None

    def get_recent_category_news(
        self,
        categories: list[str],
        lookback_hours: int = 24,
    ) -> list[dict]:
        """カテゴリ指定で直近ニュース分析を取得する。"""
        since_ts = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()
        try:
            if len(categories) == 1:
                where = {
                    "$and": [
                        {"category": {"$eq": categories[0]}},
                        {"collected_ts": {"$gte": since_ts}},
                    ]
                }
            else:
                where = {
                    "$and": [
                        {"category": {"$in": categories}},
                        {"collected_ts": {"$gte": since_ts}},
                    ]
                }
            results = self._news.get(
                where=where,
                include=["documents", "metadatas"],
            )
        except Exception:
            return []

        entries = []
        for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
            entries.append({"text": doc, "metadata": meta})
        entries.sort(key=lambda x: x["metadata"].get("collected_ts", 0), reverse=True)
        return entries

    # ---- News ---------------------------------------------------------------

    def upsert_news(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        pair: str,
        sentiment_score: float,
        confidence: float,
        key_themes: list[str],
        summary: str,
        collected_at: datetime,
    ) -> None:
        self._news.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "pair": pair,
                "sentiment_score": sentiment_score,
                "confidence": confidence,
                "key_themes": ", ".join(key_themes),
                "summary": summary,
                "collected_at": collected_at.isoformat(),
                "collected_ts": collected_at.timestamp(),
            }],
        )
        logger.debug(f"Upserted news entry {entry_id} for {pair}")

    def query_news(
        self,
        query_embedding: list[float],
        pair: str,
        top_k: int = 5,
        lookback_hours: int = 24,
    ) -> list[dict]:
        """類似ニュースをベクトル検索（時間フィルタ付き）。"""
        since_ts = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()
        try:
            results = self._news.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, max(self._news.count(), 1)),
                where={
                    "$and": [
                        {"pair": {"$eq": pair}},
                        {"collected_ts": {"$gte": since_ts}},
                    ]
                },
            )
        except Exception:
            # フィルタ結果が0件だとchromadbがエラーを返す場合があるため
            return []

        entries = []
        for i, doc in enumerate(results.get("documents", [[]])[0]):
            meta = results["metadatas"][0][i]
            entries.append({"text": doc, "metadata": meta})
        return entries

    def get_recent_news(self, pair: str, lookback_hours: int = 24) -> list[dict]:
        """時刻ベースで直近ニュースを全件取得（ベクトル検索なし）。"""
        since_ts = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()
        try:
            results = self._news.get(
                where={
                    "$and": [
                        {"pair": {"$eq": pair}},
                        {"collected_ts": {"$gte": since_ts}},
                    ]
                },
                include=["documents", "metadatas"],
            )
        except Exception:
            return []

        entries = []
        for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
            entries.append({"text": doc, "metadata": meta})
        # 新しい順にソート
        entries.sort(key=lambda x: x["metadata"].get("collected_ts", 0), reverse=True)
        return entries

    # ---- Reflections --------------------------------------------------------

    def upsert_reflection(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        pair: str,
        cycle_time: datetime,
        action: str,
        outcome_summary: str,
        lesson: str,
    ) -> None:
        self._reflections.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "pair": pair,
                "cycle_time": cycle_time.isoformat(),
                "cycle_ts": cycle_time.timestamp(),
                "action": action,
                "outcome_summary": outcome_summary,
                "lesson": lesson,
            }],
        )
        logger.debug(f"Upserted reflection {entry_id} for {pair}")

    def get_recent_reflections(self, pair: str, limit: int = 3) -> list[dict]:
        """直近の振り返りサマリーを取得（新しい順）。"""
        try:
            results = self._reflections.get(
                where={"pair": {"$eq": pair}},
                include=["documents", "metadatas"],
            )
        except Exception:
            return []

        entries = []
        for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
            entries.append({"text": doc, "metadata": meta})
        entries.sort(key=lambda x: x["metadata"].get("cycle_ts", 0), reverse=True)
        return entries[:limit]
