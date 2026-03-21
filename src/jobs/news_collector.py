"""ニュースセンチメント収集ジョブ。

RSSフィードからニュースを取得 → LLM感情分析 → ベクトル化 → ChromaDB格納。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from src.analysis.news_analyzer import analyze_news_sentiment
from src.config import AppConfig
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.rag.embedder import embed_text
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


def _format_news_for_embedding(news) -> str:
    """NewsSentiment をベクトル化用テキストに変換する。"""
    themes = ", ".join(news.key_themes) if news.key_themes else "none"
    bullish = "; ".join(news.bullish_factors[:3]) if news.bullish_factors else "none"
    bearish = "; ".join(news.bearish_factors[:3]) if news.bearish_factors else "none"
    return (
        f"Pair: {news.pair}. "
        f"Summary: {news.summary}. "
        f"Themes: {themes}. "
        f"Bullish: {bullish}. "
        f"Bearish: {bearish}. "
        f"Sentiment: {news.sentiment_score:+.2f}."
    )


async def collect_news(pair_cfg, config: AppConfig, store: VectorStore, llm: LLMClient) -> None:
    """RSSニュースを収集してChromaDBに格納する。"""
    pair = pair_cfg.display_name
    news = await analyze_news_sentiment(
        pair_cfg=pair_cfg,
        llm=llm,
        temperature=config.llm.news_analysis.temperature,
        news_sources=config.news_sources,
    )

    logger.info(
        f"[COLLECT] {pair}: news done | "
        f"items={news.news_count} score={news.sentiment_score:+.2f} conf={news.confidence:.2f} | {news.summary}"
    )

    text = _format_news_for_embedding(news)
    embedding = await embed_text(
        text=text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )
    entry_id = f"{pair_cfg.symbol}_{datetime.now().strftime('%Y%m%d%H%M')}"
    store.upsert_news(
        entry_id=entry_id,
        text=text,
        embedding=embedding,
        pair=pair_cfg.symbol,
        sentiment_score=news.sentiment_score,
        confidence=news.confidence,
        key_themes=news.key_themes,
        summary=news.summary,
        collected_at=news.fetched_at,
    )
    logger.info(f"[COLLECT] {pair}: news stored to RAG | id={entry_id}")


async def collect_all_news(config: AppConfig, store: VectorStore) -> None:
    """全有効ペアのニュースを収集してストアに格納する。"""
    llm_news = create_llm_client(config, "news_analysis")
    pairs = config.enabled_pairs
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(
        f"=== News collection started ({len(pairs)} pairs, {delay}s interval) | "
        f"llm={type(llm_news).__name__}({llm_news.model_name}) ==="
    )
    for i, pair_cfg in enumerate(pairs):
        try:
            logger.info(f"[COLLECT] {pair_cfg.display_name}: starting news collection...")
            await collect_news(pair_cfg, config, store, llm_news)
        except Exception as e:
            logger.error(f"[COLLECT] {pair_cfg.display_name}: news collection failed: {e}", exc_info=True)
        if i < len(pairs) - 1:
            logger.debug(f"[COLLECT] waiting {delay}s before next pair...")
            await asyncio.sleep(delay)
    logger.info("=== News collection complete ===")


def run_news_collection(config: AppConfig, store: VectorStore) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_news(config, store))
