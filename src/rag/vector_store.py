from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import chromadb

from src.rag.directional_store import DirectionalStore

logger = logging.getLogger(__name__)

# ChromaDB コレクション名
_NEWS_COL = "fx_news"
_REFLECTION_COL = "fx_reflections"
_INSIGHT_COL = "fx_insights"
_ECON_ANALYSIS_COL = "fx_econ_event_analysis"


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
        self._insights = self._client.get_or_create_collection(
            name=_INSIGHT_COL,
            metadata={"hnsw:space": "cosine"},
        )
        self._econ_analyses = self._client.get_or_create_collection(
            name=_ECON_ANALYSIS_COL,
            metadata={"hnsw:space": "cosine"},
        )
        self.directional = DirectionalStore(db_path)
        logger.info(
            f"VectorStore ready at {db_path} "
            f"(news={self._news.count()}, reflections={self._reflections.count()}, "
            f"insights={self._insights.count()}, econ={self._econ_analyses.count()})"
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
        newest_article_ts: float | None = None,
        articles_fingerprint: str = "",
        article_title_hashes: str = "",
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
                "newest_article_ts": newest_article_ts if newest_article_ts is not None else -1.0,
                "articles_fingerprint": articles_fingerprint,
                "article_title_hashes": article_title_hashes,
            }],
        )
        logger.debug(f"Upserted category news {entry_id} ({category})")

    def get_last_newest_article_ts(self, category: str) -> float | None:
        """前回収集時の最新記事タイムスタンプを返す（日時不明記事のみだった場合は None）。"""
        ts, _, _ = self.get_last_analysis_state(category)
        return ts

    def get_last_analysis_state(self, category: str) -> tuple[float | None, str | None, frozenset[str]]:
        """前回収集時の (newest_article_ts, articles_fingerprint, title_hashes) を返す。"""
        try:
            results = self._news.get(
                where={"category": {"$eq": category}},
                include=["metadatas"],
            )
        except Exception:
            return None, None, frozenset()
        entries = sorted(
            results.get("metadatas", []),
            key=lambda m: m.get("collected_ts", 0),
            reverse=True,
        )
        if not entries:
            return None, None, frozenset()
        latest = entries[0]
        val = latest.get("newest_article_ts", -1.0)
        ts = val if val > 0 else None
        fingerprint = latest.get("articles_fingerprint") or None
        hashes_csv = latest.get("article_title_hashes", "")
        title_hashes = frozenset(hashes_csv.split(",")) if hashes_csv else frozenset()
        return ts, fingerprint, title_hashes

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
        close_reason: str | None = None,
    ) -> None:
        metadata: dict = {
            "pair": pair,
            "cycle_time": cycle_time.isoformat(),
            "cycle_ts": cycle_time.timestamp(),
            "action": action,
            "outcome_summary": outcome_summary,
            "lesson": lesson,
        }
        if close_reason is not None:
            metadata["close_reason"] = close_reason
        self._reflections.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
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

    # ---- Insights -----------------------------------------------------------

    def upsert_insight(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        pair: str,
        insight_type: str,  # "analysis" | "pattern" | "risk" | "general"
        source_question: str,
        created_at: datetime,
    ) -> None:
        """ask回答から抽出した洞察をRAGに保存する。"""
        metadata = {
            "pair": pair,
            "insight_type": insight_type,
            "source_question": source_question[:200],
            "created_at": created_at.isoformat(),
            "created_ts": created_at.timestamp(),
        }
        self._insights.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        logger.info(f"[INSIGHT] Stored: {entry_id} | {pair} | {insight_type}")

    def get_recent_insights(
        self,
        pair: str | None = None,
        limit: int = 5,
        lookback_hours: int = 72,
    ) -> list[dict]:
        """直近の洞察を取得する。"""
        if self._insights.count() == 0:
            return []
        cutoff_ts = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()
        where: dict | None = {"created_ts": {"$gte": cutoff_ts}}
        if pair:
            where = {"$and": [
                {"pair": {"$eq": pair}},
                {"created_ts": {"$gte": cutoff_ts}},
            ]}
        try:
            results = self._insights.get(
                where=where,
                limit=limit,
            )
        except Exception:
            results = self._insights.get(limit=limit)
        entries = []
        for i, doc in enumerate(results.get("documents", [])):
            entries.append({
                "text": doc,
                "metadata": results["metadatas"][i] if results.get("metadatas") else {},
            })
        # Sort by created_ts descending
        entries.sort(key=lambda e: e["metadata"].get("created_ts", 0), reverse=True)
        return entries[:limit]

    def query_insights(
        self,
        query_embedding: list[float],
        pair: str | None = None,
        top_k: int = 3,
        lookback_hours: int = 72,
    ) -> list[dict]:
        """セマンティック検索で関連する洞察を取得する。"""
        if self._insights.count() == 0:
            return []
        cutoff_ts = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()
        where: dict | None = {"created_ts": {"$gte": cutoff_ts}}
        if pair:
            where = {"$and": [
                {"pair": {"$eq": pair}},
                {"created_ts": {"$gte": cutoff_ts}},
            ]}
        n = min(top_k, self._insights.count())
        try:
            results = self._insights.query(
                query_embeddings=[query_embedding],
                n_results=n,
                where=where,
            )
        except Exception:
            results = self._insights.query(
                query_embeddings=[query_embedding],
                n_results=n,
            )
        entries = []
        for i, doc in enumerate(results.get("documents", [[]])[0]):
            entries.append({
                "text": doc,
                "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                "distance": results["distances"][0][i] if results.get("distances") else 0.5,
            })
        return entries

    # ---- Maintenance --------------------------------------------------------

    def cleanup_old_news(self, max_age_hours: int = 48) -> int:
        """指定時間より古いニュースエントリを削除する（リフレクションは対象外）。

        Returns:
            削除したエントリ数
        """
        cutoff_ts = (datetime.now() - timedelta(hours=max_age_hours)).timestamp()
        try:
            results = self._news.get(
                where={"collected_ts": {"$lt": cutoff_ts}},
                include=["metadatas"],
            )
        except Exception:
            return 0

        ids = results.get("ids", [])
        if not ids:
            return 0

        self._news.delete(ids=ids)
        logger.info(f"[VectorStore] cleanup: deleted {len(ids)} news entries older than {max_age_hours}h")
        return len(ids)

    # ---- Economic Event Analysis ---------------------------------

    def upsert_econ_analysis(
        self,
        event_id: str,
        text: str,
        embedding: list[float],
        title: str,
        currency: str,
        importance: int,
        event_time: datetime,
        actual: float | None,
        forecast: float | None,
        surprise: str | None,
        analyzed_at: datetime,
    ) -> None:
        """経済指標イベント分析をRAGに保存する。"""
        metadata = {
            "event_id": event_id,
            "title": title[:200],
            "currency": currency,
            "importance": importance,
            "event_time": event_time.isoformat(),
            "event_ts": event_time.timestamp(),
            "actual": actual if actual is not None else 0.0,
            "forecast": forecast if forecast is not None else 0.0,
            "surprise": surprise or "unknown",
            "analyzed_at": analyzed_at.isoformat(),
        }
        self._econ_analyses.upsert(
            ids=[event_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        logger.info(f"[ECON-RAG] Stored: {event_id} | {currency} | {title[:40]}")

    def get_recent_econ_analyses(
        self,
        currencies: list[str] | None = None,
        lookback_minutes: int = 30,
        limit: int = 5,
    ) -> list[dict]:
        """直近の経済指標分析を取得する。"""
        if self._econ_analyses.count() == 0:
            return []
        cutoff_ts = (datetime.now() - timedelta(minutes=lookback_minutes)).timestamp()
        where: dict = {"event_ts": {"$gte": cutoff_ts}}
        if currencies:
            where = {"$and": [
                {"currency": {"$in": currencies}},
                {"event_ts": {"$gte": cutoff_ts}},
            ]}
        try:
            results = self._econ_analyses.get(where=where, limit=limit)
        except Exception:
            results = self._econ_analyses.get(limit=limit)
        entries = []
        for i, doc in enumerate(results.get("documents", [])):
            entries.append({
                "text": doc,
                "metadata": results["metadatas"][i] if results.get("metadatas") else {},
            })
        entries.sort(key=lambda e: e["metadata"].get("event_ts", 0), reverse=True)
        return entries[:limit]
