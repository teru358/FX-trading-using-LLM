"""カテゴリ別ニュース収集ジョブ。

3カテゴリ（FX / Global / Japan）ごとに:
  RSS取得 → LLMセンチメント分析 → ベクトル化 → ChromaDB格納。
取引判定時にペア別の関連カテゴリを集約して使用する。
"""

from __future__ import annotations

import asyncio
import logging
from src.utils.clock import db_now

from src.analysis.feedly_fetcher import fetch_feedly_category
from src.analysis.article_fetcher import fetch_bodies_concurrent
from src.analysis.news_analyzer import NewsSentiment, analyze_category_sentiment
from src.analysis.price_analyzer import load_user_notes
from src.analysis.rss_fetcher import fetch_category_news, title_hash
from src.config import AppConfig
from src.llm.factory import create_llm_client
from src.rag.embedder import make_embed_fn
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# カテゴリ別キーワード（None = フィルタなし、FX専門フィードなので全記事対象）

_CATEGORY_LABELS = {"fx": "FX Market", "global": "Global Economy", "japan": "Japan / JPY"}


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
    new_hashes = (current_hashes - prev_hashes) if prev_hashes else current_hashes
    new_count = len(new_hashes)
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

    # 新規ニュースが0件ならLLM分析をスキップ (fingerprintが僅かに変わっても、
    # new=0 なら分析結果は前回と実質変わらないため、LLMリソースを無駄にしない)
    if last_fingerprint is not None and new_count == 0:
        logger.info(f"[NEWS] {label}: new=0 (no new articles), skipping LLM")
        return

    # Deep fetch: 新規記事のみ trafilatura で本文取得 (既知記事は body=None のまま)
    if config.news_collection.deep_fetch_enabled and new_hashes:
        new_items = [item for item in fetch_result.items if title_hash(item.title) in new_hashes]
        with_link = [it for it in new_items if it.link]
        if with_link:
            await fetch_bodies_concurrent(
                with_link,
                max_concurrent=config.news_collection.deep_fetch_max_concurrent,
                timeout_seconds=config.news_collection.deep_fetch_timeout_seconds,
                max_chars=config.news_collection.deep_fetch_max_chars,
                user_agent=config.news_collection.deep_fetch_user_agent,
            )
            success = sum(1 for it in with_link if it.body)
            skipped = len(new_items) - len(with_link)
            msg = f"deep fetch {success}/{len(with_link)} succeeded"
            if skipped:
                msg += f" ({skipped} skipped: no link)"
            logger.info(f"[NEWS] {label}: {msg}")

    # 方向別RAGから類似テーマの取引振り返りを検索
    trade_lessons = ""
    embed_fn = make_embed_fn(config)
    try:
        news_text_for_search = fetch_result.format_for_llm()[:500]
        query_embedding = await embed_fn(text=news_text_for_search)
        # forecast/hold カードと trade entry カードは退役済み (spec §3.4b)。
        # 完結した実取引の振り返りのみを教訓として注入する。
        bullish_hits = store.directional.query(
            query_embedding, "bullish", top_k=3,
            phase_filter="complete", session_type_filter="trade")
        bearish_hits = store.directional.query(
            query_embedding, "bearish", top_k=3,
            phase_filter="complete", session_type_filter="trade")
        all_hits = bullish_hits + bearish_hits
        all_hits.sort(key=lambda h: h.get("distance", float("inf")))
        top_hits = all_hits[:3]
        if top_hits:
            lines = ["=== Past Trade Lessons (related) ==="]
            for h in top_hits:
                text = h.get("text", "")[:200]
                lines.append(text)
            trade_lessons = "\n".join(lines)
            logger.info(f"[NEWS] {label}: injecting {len(top_hits)} trade lessons from directional RAG")
    except Exception as e:
        logger.debug(f"[NEWS] {label}: trade lessons search failed: {e}")

    # LLM分析
    news = await analyze_category_sentiment(
        category=category,
        fetch_result=fetch_result,
        llm=llm,
        temperature=config.llm.news_analysis.temperature,
        user_notes=load_user_notes(config.user_notes_path, "news"),
        trade_lessons=trade_lessons,
    )

    collect_undated = news.news_count - news.recent_count
    collect_undated_part = f"+{collect_undated}?" if collect_undated > 0 else ""
    logger.info(
        f"[COLLECT] {label}: news done | "
        f"items={news.news_count} (recent={news.recent_count}{collect_undated_part}) "
        f"feeds={news.feeds_ok}/{news.feeds_ok + news.feeds_failed} "
        f"score={news.sentiment_score:+.2f} conf={news.confidence:.2f} | {news.summary}"
    )

    # ベクトル化してRAGに格納。embed / upsert 失敗時も LLM 分析結果を捨てない
    # (次カテゴリ処理は続行する)。fingerprint はこのカテゴリでは保存されないため、
    # 次サイクルで同じ記事セットに対する再分析は起こるが、LLM 分析結果自体はログに残る。
    text = _format_for_embedding(category, news)
    try:
        embedding = await embed_fn(text=text)
        entry_id = f"{category}_{db_now().strftime('%Y%m%d%H%M')}"
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
            collected_at=db_now(),
            newest_article_ts=fetch_result.newest_published_ts,
            articles_fingerprint=fetch_result.articles_fingerprint,
            article_title_hashes=fetch_result.title_hashes_csv,
        )
        logger.debug(f"[COLLECT] {label}: stored to RAG | id={entry_id}")
    except Exception as e:
        logger.warning(
            f"[COLLECT] {label}: RAG storage failed ({type(e).__name__}: {e}) — "
            f"analysis kept in logs, will re-run next cycle"
        )


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


def run_news_collection(config: AppConfig, store: VectorStore) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_news(config, store))
