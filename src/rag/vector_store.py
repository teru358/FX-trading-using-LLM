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


# ── 内部ヘルパー ───────────────────────────────────────────────

def _cutoff_ts(*, hours: int | None = None, minutes: int | None = None) -> float:
    """直近 N 時間 / 分の境界 Unix タイムスタンプを返す。"""
    if hours is not None:
        delta = timedelta(hours=hours)
    elif minutes is not None:
        delta = timedelta(minutes=minutes)
    else:
        raise ValueError("hours または minutes のどちらかを指定してください")
    return (datetime.now() - delta).timestamp()


def _unpack_get(results: dict) -> list[dict]:
    """chromadb ``collection.get()`` の結果を ``[{"text", "metadata"}]`` に変換する。"""
    docs = results.get("documents", []) or []
    metas = results.get("metadatas", []) or []
    return [{"text": doc, "metadata": meta} for doc, meta in zip(docs, metas)]


def _unpack_query(results: dict) -> list[dict]:
    """chromadb ``collection.query()`` (単一バッチ) の結果を
    ``[{"text", "metadata", "distance"}]`` に変換する。"""
    docs_batch = results.get("documents") or [[]]
    metas_batch = results.get("metadatas") or [[]]
    dists_batch = results.get("distances") or [[]]
    docs = docs_batch[0] if docs_batch else []
    metas = metas_batch[0] if metas_batch else []
    dists = dists_batch[0] if dists_batch else []
    entries: list[dict] = []
    for i, doc in enumerate(docs):
        entries.append({
            "text": doc,
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dists[i] if i < len(dists) else 0.5,
        })
    return entries


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
        cat_filter = (
            {"category": {"$eq": categories[0]}}
            if len(categories) == 1
            else {"category": {"$in": categories}}
        )
        where = {
            "$and": [
                cat_filter,
                {"collected_ts": {"$gte": _cutoff_ts(hours=lookback_hours)}},
            ]
        }
        try:
            results = self._news.get(where=where, include=["documents", "metadatas"])
        except Exception:
            return []

        entries = _unpack_get(results)
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
        entries = _unpack_get(results)
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

    def _insights_where(self, pair: str | None, lookback_hours: int) -> dict:
        """insights 用の where 句 (時間フィルタ + 任意の pair フィルタ) を構築する。"""
        ts_filter = {"created_ts": {"$gte": _cutoff_ts(hours=lookback_hours)}}
        if pair:
            return {"$and": [{"pair": {"$eq": pair}}, ts_filter]}
        return ts_filter

    def get_recent_insights(
        self,
        pair: str | None = None,
        limit: int = 5,
        lookback_hours: int = 72,
    ) -> list[dict]:
        """直近の洞察を取得する。"""
        if self._insights.count() == 0:
            return []
        where = self._insights_where(pair, lookback_hours)
        try:
            results = self._insights.get(where=where, limit=limit)
        except Exception:
            results = self._insights.get(limit=limit)
        entries = _unpack_get(results)
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
        where = self._insights_where(pair, lookback_hours)
        n = min(top_k, self._insights.count())
        try:
            results = self._insights.query(
                query_embeddings=[query_embedding], n_results=n, where=where,
            )
        except Exception:
            results = self._insights.query(
                query_embeddings=[query_embedding], n_results=n,
            )
        return _unpack_query(results)

    # ---- Maintenance --------------------------------------------------------

    def cleanup_old_news(self, max_age_hours: int = 48) -> int:
        """指定時間より古いニュースエントリを削除する（リフレクションは対象外）。

        Returns:
            削除したエントリ数
        """
        try:
            results = self._news.get(
                where={"collected_ts": {"$lt": _cutoff_ts(hours=max_age_hours)}},
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
        ts_filter = {"event_ts": {"$gte": _cutoff_ts(minutes=lookback_minutes)}}
        where: dict = (
            {"$and": [{"currency": {"$in": currencies}}, ts_filter]}
            if currencies else ts_filter
        )
        try:
            results = self._econ_analyses.get(where=where, limit=limit)
        except Exception:
            results = self._econ_analyses.get(limit=limit)
        entries = _unpack_get(results)
        entries.sort(key=lambda e: e["metadata"].get("event_ts", 0), reverse=True)
        return entries[:limit]
