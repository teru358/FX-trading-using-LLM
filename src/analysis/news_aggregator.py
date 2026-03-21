from __future__ import annotations

import logging

from src.analysis.news_analyzer import NewsSentiment
from src.config import AppConfig
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


def aggregate_news_sentiment(pair_cfg, store: VectorStore, config: AppConfig) -> NewsSentiment:
    """直近RAGニュースの平均センチメントから NewsSentiment を構築する。"""
    entries = store.get_recent_news(pair_cfg.symbol, lookback_hours=config.rag.news_lookback_hours)
    if not entries:
        return NewsSentiment(pair=pair_cfg.symbol, sentiment_score=0.0, confidence=0.3)

    scores = [float(e["metadata"].get("sentiment_score", 0)) for e in entries]
    confs = [float(e["metadata"].get("confidence", 0.5)) for e in entries]
    themes_raw = [e["metadata"].get("key_themes", "") for e in entries]

    avg_score = sum(scores) / len(scores)
    avg_conf = sum(confs) / len(confs)
    all_themes = list({t.strip() for raw in themes_raw for t in raw.split(",") if t.strip()})

    return NewsSentiment(
        pair=pair_cfg.symbol,
        sentiment_score=avg_score,
        confidence=min(avg_conf, 0.9),
        key_themes=all_themes[:5],
        summary=f"Aggregated from {len(entries)} RAG entries (avg score={avg_score:+.2f})",
    )
