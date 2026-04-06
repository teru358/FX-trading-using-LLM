"""テクニカル分析スナップショットの収集ジョブ。

OHLCVデータ取得 → テクニカル指標計算 → LLM分析 → スナップショット保存。

2フェーズ実行:
  Phase 1: 監視専用銘柄（指数・参照FX）を先に収集
  Phase 2: 取引対象FXペアを収集（Phase 1 のマクロコンテキスト付き）
"""

from __future__ import annotations

import asyncio
import logging

from src.analysis.price_analyzer import analyze_price_action
from src.config import AppConfig, InstrumentConfig
from src.data.analysis_store import AnalysisStore
from src.data.correlation import PairCorrelation, compute_correlations, format_correlation_context
from src.data.indicators import compute_indicators
from src.data.price_provider import PriceProvider
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.rag.prompt_formatter import (
    format_macro_context_for_prompt,
    format_news_for_prompt,
    format_previous_analysis_for_prompt,
    format_reflections_for_prompt,
)
from src.rag.vector_store import VectorStore
from src.trading.market_hours import is_market_open, market_status_label

logger = logging.getLogger(__name__)


async def _collect_one(
    inst: InstrumentConfig,
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
    macro_context: str = "",
    correlation_context: str = "",
    price_provider: "PriceProvider | None" = None,
) -> None:
    """1銘柄のOHLCVを取得してテクニカル分析を実行し、スナップショットを保存する。"""
    from datetime import datetime, timedelta

    if price_provider:
        price_data = price_provider.get_ohlcv(
            inst.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )
    else:
        from src.data.price_fetcher import fetch_ohlcv
        price_data = fetch_ohlcv(
            inst.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )

    # 価格データの鮮度チェック: 最新バーが古すぎる場合LLM呼び出しをスキップ
    latest_bar = price_data.df.index[-1]
    if hasattr(latest_bar, "to_pydatetime"):
        latest_bar = latest_bar.to_pydatetime()
    if hasattr(latest_bar, "tzinfo") and latest_bar.tzinfo is not None:
        latest_bar = latest_bar.replace(tzinfo=None)
    staleness = datetime.now() - latest_bar
    max_staleness = timedelta(hours=6)
    if staleness > max_staleness:
        logger.info(
            f"[COLLECT] {inst.display_name}: stale data (latest bar {staleness} ago), skipping LLM analysis"
        )
        return

    _, summary = compute_indicators(
        price_data.df,
        indicator_cfg=config.analysis.indicators,
        pattern_cfg=config.analysis.chart_patterns,
    )

    # RAGからコンテキストを構築
    news_entries = store.get_recent_category_news(
        categories=inst.news_categories,
        lookback_hours=config.rag.news_lookback_hours,
    )
    reflections = store.get_recent_reflections(
        pair=inst.symbol,
        limit=config.rag.reflection_lookback_count,
    )
    news_ctx = format_news_for_prompt(news_entries)
    refl_ctx = format_reflections_for_prompt(reflections)

    # 前回分析スナップショット（直近1件）
    prev_snapshots = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
    prev_ctx = format_previous_analysis_for_prompt(prev_snapshots[0] if prev_snapshots else None)

    # マクロコンテキストに相関データを付加
    full_macro = macro_context
    if correlation_context:
        full_macro = f"{macro_context}\n\n{correlation_context}" if macro_context else correlation_context

    price_analysis = await analyze_price_action(
        pair_cfg=inst,
        price_data=price_data,
        summary=summary,
        llm=llm,
        temperature=config.llm.price_analysis.temperature,
        news_context=news_ctx,
        reflection_context=refl_ctx,
        previous_analysis=prev_ctx,
        macro_context=full_macro,
        user_notes_path=config.user_notes_path,
    )
    analysis_store.upsert_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )


async def collect_all_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """全有効銘柄のテクニカル分析を2フェーズで収集してストアに格納する。"""
    if not force and not is_market_open():
        logger.info(f"Market {market_status_label()}. Skipping technical collection.")
        return

    llm_price = create_llm_client(config, "price_analysis")
    watch_only = config.watch_only_instruments
    tradeable = config.tradeable_instruments
    all_instruments = watch_only + tradeable
    delay = config.news_collection.inter_pair_delay_seconds

    logger.info(
        f"=== Technical collection started "
        f"({len(tradeable)} trade + {len(watch_only)} watch, {delay}s interval) | "
        f"llm={type(llm_price).__name__}({llm_price.model_name}) ==="
    )

    # Phase 1: 監視専用銘柄（指数・参照FX）を先に収集
    if watch_only:
        logger.info(f"[COLLECT] Phase 1: {len(watch_only)} watch-only instruments")
    for i, inst in enumerate(watch_only):
        try:
            logger.debug(f"[COLLECT] {inst.display_name}: starting OHLCV + technical analysis...")
            await _collect_one(inst, config, store, price_store, analysis_store, llm_price, price_provider=price_provider)
        except Exception as e:
            logger.error(f"[COLLECT] {inst.display_name}: technical analysis failed: {e}", exc_info=True)
        if i < len(watch_only) - 1:
            await asyncio.sleep(delay)

    # Phase 1 の結果からマクロコンテキストを構築
    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    macro_ctx = format_macro_context_for_prompt(
        macro_snapshots, watch_only,
        realtime_provider=config.price_provider.realtime_provider,
    )

    # Phase 1.5: trade×watch の価格相関を計算（LLMなし）
    correlations: list[PairCorrelation] = []
    if watch_only and tradeable:
        try:
            from src.data.price_fetcher import fetch_ohlcv as _fetch_ohlcv
            _period = f"{config.trading.lookback_days}d"
            _interval = config.trading.ohlcv_interval

            trade_prices = {}
            for inst in tradeable:
                try:
                    pd_ = (price_provider.get_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store)
                           if price_provider else _fetch_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store))
                    trade_prices[inst.symbol] = pd_
                except Exception as e:
                    logger.warning(f"[CORR] {inst.display_name}: OHLCV fetch failed: {e}")

            watch_prices = {}
            for inst in watch_only:
                try:
                    pd_ = (price_provider.get_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store)
                           if price_provider else _fetch_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store))
                    watch_prices[inst.symbol] = pd_
                except Exception as e:
                    logger.warning(f"[CORR] {inst.display_name}: OHLCV fetch failed: {e}")

            watch_names = {inst.symbol: inst.display_name for inst in watch_only}
            correlations = compute_correlations(trade_prices, watch_prices, watch_names)
            logger.info(f"[CORR] Computed {len(correlations)} correlation pairs")
        except Exception as e:
            logger.error(f"[CORR] Correlation computation failed: {e}", exc_info=True)

    # Phase 2: 取引対象FXペアを収集（マクロコンテキスト + 相関データ付き）
    if tradeable:
        if watch_only:
            await asyncio.sleep(delay)
        logger.info(f"[COLLECT] Phase 2: {len(tradeable)} tradeable instruments")
    for i, inst in enumerate(tradeable):
        try:
            corr_ctx = format_correlation_context(correlations, inst.symbol)
            logger.debug(f"[COLLECT] {inst.display_name}: starting OHLCV + technical analysis...")
            await _collect_one(inst, config, store, price_store, analysis_store, llm_price,
                               macro_context=macro_ctx, correlation_context=corr_ctx, price_provider=price_provider)
        except Exception as e:
            logger.error(f"[COLLECT] {inst.display_name}: technical analysis failed: {e}", exc_info=True)
        if i < len(tradeable) - 1:
            await asyncio.sleep(delay)

    logger.info("=== Technical collection complete ===")


def run_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_technical(config, store, price_store, analysis_store, force=force, price_provider=price_provider))
