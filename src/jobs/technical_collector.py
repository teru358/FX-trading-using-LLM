"""テクニカル分析スナップショットの収集ジョブ。

OHLCVデータ取得 → テクニカル指標計算 → LLM分析 → スナップショット保存。
"""

from __future__ import annotations

import asyncio
import logging

from src.analysis.price_analyzer import analyze_price_action
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.indicators import compute_indicators
from src.data.price_fetcher import fetch_ohlcv
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.rag.prompt_formatter import format_news_for_prompt, format_reflections_for_prompt
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


async def collect_technical(
    pair_cfg,
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
) -> None:
    """OHLCVを取得してテクニカル分析を実行し、スナップショットを保存する。"""
    pair = pair_cfg.display_name
    price_data = fetch_ohlcv(
        pair_cfg.symbol,
        period=f"{config.trading.lookback_days}d",
        interval=config.trading.ohlcv_interval,
        price_store=price_store,
    )
    _, summary = compute_indicators(price_data.df)

    # RAGからコンテキストを構築（直前のニュース収集結果を含む）
    news_entries = store.get_recent_news(
        pair=pair_cfg.symbol,
        lookback_hours=config.rag.news_lookback_hours,
    )
    reflections = store.get_recent_reflections(
        pair=pair_cfg.symbol,
        limit=config.rag.reflection_lookback_count,
    )
    news_ctx = format_news_for_prompt(news_entries)
    refl_ctx = format_reflections_for_prompt(reflections)

    price_analysis = await analyze_price_action(
        pair_cfg=pair_cfg,
        price_data=price_data,
        summary=summary,
        llm=llm,
        temperature=config.llm.price_analysis.temperature,
        news_context=news_ctx,
        reflection_context=refl_ctx,
        user_notes_path=config.user_notes_path,
    )
    analysis_store.upsert_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {pair}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )


async def collect_all_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
) -> None:
    """全有効ペアのテクニカル分析を収集してストアに格納する。"""
    llm_price = create_llm_client(config, "price_analysis")
    pairs = config.enabled_pairs
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(
        f"=== Technical collection started ({len(pairs)} pairs, {delay}s interval) | "
        f"llm={type(llm_price).__name__}({llm_price.model_name}) ==="
    )
    for i, pair_cfg in enumerate(pairs):
        try:
            logger.info(f"[COLLECT] {pair_cfg.display_name}: starting OHLCV + technical analysis...")
            await collect_technical(pair_cfg, config, store, price_store, analysis_store, llm_price)
        except Exception as e:
            logger.error(f"[COLLECT] {pair_cfg.display_name}: technical analysis failed: {e}", exc_info=True)
        if i < len(pairs) - 1:
            logger.debug(f"[COLLECT] waiting {delay}s before next pair...")
            await asyncio.sleep(delay)
    logger.info("=== Technical collection complete ===")


def run_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_technical(config, store, price_store, analysis_store))
