"""カテゴリ別ニュース収集ジョブ。

3カテゴリ（FX / Global / Japan）ごとに:
  RSS取得 → LLMセンチメント分析 → ベクトル化 → ChromaDB格納。
取引判定時にペア別の関連カテゴリを集約して使用する。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from src.analysis.feedly_fetcher import fetch_feedly_category
from src.analysis.news_analyzer import NewsSentiment, analyze_category_sentiment
from src.analysis.price_analyzer import load_user_notes
from src.analysis.rss_fetcher import fetch_category_news
from src.config import AppConfig
from src.llm.factory import create_llm_client
from src.rag.embedder import embed_text
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# カテゴリ別キーワード（None = フィルタなし、FX専門フィードなので全記事対象）

_CATEGORY_LABELS = {"fx": "FX Market", "global": "Global Economy", "japan": "Japan / JPY"}

_last_cleanup: datetime | None = None
_CLEANUP_INTERVAL = timedelta(hours=24)
_CLEANUP_MAX_AGE_HOURS = 48


def _format_for_embedding(category: str, news: NewsSentiment) -> str:
    """カテゴリ分析結果をベクトル化用テキストに変換する。"""
    themes = ", ".join(news.key_themes) if news.key_themes else "none"
    bullish = "; ".join(news.bullish_factors[:3]) if news.bullish_factors else "none"
    bearish = "; ".join(news.bearish_factors[:3]) if news.bearish_factors else "none"
    return (
        f"Category: {category}. "
        f"Summary: {news.summary}. "
        f"Themes: {themes}. "
        f"Bullish: {bullish}. "
        f"Bearish: {bearish}. "
        f"Sentiment: {news.sentiment_score:+.2f}."
    )


async def collect_category(
    category: str,
    feeds: list[str],
    keywords: frozenset[str] | None,
    config: AppConfig,
    store: VectorStore,
    llm,
) -> None:
    """1カテゴリのニュースを収集・分析してRAGに格納する。"""
    label = _CATEGORY_LABELS.get(category, category)

    feedly = config.news_sources.feedly
    feedly_streams = feedly.streams_for(category)
    if feedly.enabled and feedly.access_token and feedly_streams:
        logger.debug(f"[NEWS] {label}: Feedly API を使用 ({len(feedly_streams)} streams)")
        fetch_result = fetch_feedly_category(
            stream_ids=feedly_streams,
            access_token=feedly.access_token,
            freshness_hours=config.news_collection.news_freshness_hours,
            count=feedly.count,
            summary_max_chars=config.news_collection.summary_max_chars,
        )
    else:
        fetch_result = fetch_category_news(
            feeds=feeds,
            keywords=keywords,
            freshness_hours=config.news_collection.news_freshness_hours,
            summary_max_chars=config.news_collection.summary_max_chars,
        )

    # 前回のタイトルハッシュと比較して new/known を算出
    _, last_fingerprint, prev_hashes = store.get_last_analysis_state(category)
    current_hashes = fetch_result.title_hashes
    current_fingerprint = fetch_result.articles_fingerprint
    new_count = len(current_hashes - prev_hashes) if prev_hashes else len(current_hashes)
    known_count = len(current_hashes & prev_hashes) if prev_hashes else 0

    if fetch_result.items:
        undated_count = fetch_result.news_count - fetch_result.recent_count
        undated_part = f" undated={undated_count}" if undated_count > 0 else ""
        logger.info(
            f"[NEWS] {label}: fetched {fetch_result.news_count} items "
            f"(new={new_count} known={known_count} recent={fetch_result.recent_count}{undated_part}) "
            f"from {fetch_result.feeds_ok}/{fetch_result.total_feeds} feeds\n"
            + fetch_result.format_titles_log()
        )
    else:
        logger.info(
            f"[NEWS] {label}: no items "
            f"(feeds OK={fetch_result.feeds_ok} failed={fetch_result.feeds_failed})"
        )

    # 前回分析時と記事セットが同じであればスキップ
    if last_fingerprint is not None and current_fingerprint == last_fingerprint:
        logger.info(f"[NEWS] {label}: no new articles since last analysis, skipping LLM")
        return

    # LLM分析
    news = await analyze_category_sentiment(
        category=category,
        fetch_result=fetch_result,
        llm=llm,
        temperature=config.llm.news_analysis.temperature,
        user_notes=load_user_notes(config.user_notes_path, "news"),
    )

    collect_undated = news.news_count - news.recent_count
    collect_undated_part = f"+{collect_undated}?" if collect_undated > 0 else ""
    logger.info(
        f"[COLLECT] {label}: news done | "
        f"items={news.news_count} (recent={news.recent_count}{collect_undated_part}) "
        f"feeds={news.feeds_ok}/{news.feeds_ok + news.feeds_failed} "
        f"score={news.sentiment_score:+.2f} conf={news.confidence:.2f} | {news.summary}"
    )

    # ベクトル化してRAGに格納
    text = _format_for_embedding(category, news)
    embedding = await embed_text(
        text=text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )
    entry_id = f"{category}_{datetime.now().strftime('%Y%m%d%H%M')}"
    store.upsert_category_news(
        entry_id=entry_id,
        text=text,
        embedding=embedding,
        category=category,
        sentiment_score=news.sentiment_score,
        confidence=news.confidence,
        key_themes=news.key_themes,
        summary=news.summary,
        news_count=news.news_count,
        collected_at=datetime.now(),
        newest_article_ts=fetch_result.newest_published_ts,
        articles_fingerprint=fetch_result.articles_fingerprint,
        article_title_hashes=fetch_result.title_hashes_csv,
    )
    logger.debug(f"[COLLECT] {label}: stored to RAG | id={entry_id}")


async def collect_all_news(config: AppConfig, store: VectorStore) -> None:
    """全カテゴリのニュースを収集・分析してストアに格納する。"""
    llm_news = create_llm_client(config, "news_analysis")

    global_kw = frozenset(kw.lower() for kw in config.keywords.global_keywords)
    japan_kw = frozenset(kw.lower() for kw in config.keywords.japan_keywords)

    categories = [
        ("fx", config.news_sources.feeds_fx, None),
        ("global", config.news_sources.feeds_global, global_kw),
        ("japan", config.news_sources.feeds_japan, japan_kw),
    ]

    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(
        f"=== News collection started ({len(categories)} categories, {delay}s interval) | "
        f"llm={type(llm_news).__name__}({llm_news.model_name}) ==="
    )
    for i, (category, feeds, keywords) in enumerate(categories):
        try:
            await collect_category(category, feeds, keywords, config, store, llm_news)
        except Exception as e:
            logger.error(
                f"[COLLECT] {_CATEGORY_LABELS.get(category, category)}: "
                f"collection failed: {e}", exc_info=True
            )
        if i < len(categories) - 1:
            logger.debug(f"[COLLECT] waiting {delay}s before next category...")
            await asyncio.sleep(delay)
    logger.info("=== News collection complete ===")

    # 古いニュースエントリのクリーンアップ（24時間に1回）
    global _last_cleanup
    now = datetime.now()
    if _last_cleanup is None or now - _last_cleanup >= _CLEANUP_INTERVAL:
        store.cleanup_old_news(max_age_hours=_CLEANUP_MAX_AGE_HOURS)
        _last_cleanup = now


def run_news_collection(config: AppConfig, store: VectorStore) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_news(config, store))
