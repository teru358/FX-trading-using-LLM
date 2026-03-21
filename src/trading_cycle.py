from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from functools import partial

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.analysis.price_analyzer import analyze_price_action
from src.analysis.reflector import generate_reflection, store_reflection
from src.config import AppConfig
from src.data.indicators import compute_indicators
from src.data.price_fetcher import fetch_current_price, fetch_ohlcv
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.embedder import embed_text
from src.rag.prompt_formatter import format_news_for_prompt, format_reflections_for_prompt
from src.rag.vector_store import VectorStore
from src.notifications.notifier import (
    OrderClosedEvent,
    OrderOpenedEvent,
    SignalSkippedEvent,
    create_notifier,
)
from src.reporting.reporter import print_run_summary
from src.signals.signal_combiner import combine_signals
from src.trading.live_broker import create_broker
from src.trading.market_hours import is_market_open, market_status_label
from src.trading.position_manager import PositionManager

logger = logging.getLogger(__name__)


async def _build_rag_context(
    pair_cfg, config: AppConfig, store: VectorStore
) -> tuple[str, str]:
    """RAGからニュースと振り返りコンテキストを取得する。"""
    news_entries = store.get_recent_news(
        pair=pair_cfg.symbol,
        lookback_hours=config.rag.news_lookback_hours,
    )
    news_ctx = format_news_for_prompt(news_entries)

    reflections = store.get_recent_reflections(
        pair=pair_cfg.symbol,
        limit=config.rag.reflection_lookback_count,
    )
    refl_ctx = format_reflections_for_prompt(reflections)

    return news_ctx, refl_ctx


async def _process_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
):
    """1ペアの分析→シグナル生成。

    テクニカル分析は収集ジョブで蓄積済みのスナップショットを集約して使用する。
    スナップショットが存在しない場合（初回起動直後など）はOllama即時分析にフォールバック。
    """
    try:
        price_data = fetch_ohlcv(
            pair_cfg.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )
        _, summary = compute_indicators(price_data.df)

        # 蓄積されたテクニカル分析スナップショットを時間加重集約
        price = analysis_store.aggregate(
            pair_cfg.symbol,
            hours=config.rag.analysis_lookback_hours,
        )
        if price is None:
            logger.warning(
                f"{pair_cfg.display_name}: no stored snapshots, "
                "running immediate Ollama analysis as fallback..."
            )
            news_ctx, refl_ctx = await _build_rag_context(pair_cfg, config, store)
            price = await analyze_price_action(
                pair_cfg=pair_cfg,
                price_data=price_data,
                summary=summary,
                llm=llm,
                temperature=config.llm.price_analysis.temperature,
                news_context=news_ctx,
                reflection_context=refl_ctx,
                user_notes_path=config.user_notes_path,
            )

        news = aggregate_news_sentiment(pair_cfg, store, config)

        account = position_mgr.get_account_state()
        signal = combine_signals(
            news=news,
            price=price,
            current_price=price_data.current_price,
            pair_cfg=pair_cfg,
            account_balance=account.balance,
            risk_per_trade=config.trading.risk_per_trade,
            confidence_threshold=config.trading.signal_confidence_threshold,
            news_weight=config.trading.news_weight,
            price_weight=config.trading.price_weight,
            signal_deadband=config.trading.signal_deadband,
            min_lot_size=config.trading.min_lot_size,
            lot_unit=config.trading.lot_unit,
        )
        return signal
    except Exception as e:
        logger.error(f"Failed to process {pair_cfg.display_name}: {e}", exc_info=True)
        return e


async def _generate_cycle_reflections(
    config: AppConfig, position_mgr: PositionManager, store: VectorStore, llm: LLMClient,
) -> None:
    """オープンポジションに対して振り返りを生成・RAGに蓄積する。"""
    for pos in position_mgr.get_account_state().open_positions:
        try:
            current_price = fetch_current_price(pos.pair)
            pair_cfg = next((p for p in config.enabled_pairs if p.symbol == pos.pair), None)
            if pair_cfg is None:
                continue

            reflection = await generate_reflection(
                pair_cfg=pair_cfg,
                previous_action=pos.direction,
                previous_entry_price=pos.entry_price,
                previous_stop_loss=pos.stop_loss,
                previous_take_profit=pos.take_profit,
                previous_reasoning=f"entry={pos.entry_price:.5f} SL={pos.stop_loss:.5f} TP={pos.take_profit:.5f}",
                previous_cycle_time=pos.opened_at,
                current_price=current_price,
                llm=llm,
                temperature=config.llm.reflection.temperature,
            )
            embed_fn = partial(
                embed_text,
                ollama_base_url=config.llm.ollama.base_url,
                model=config.rag.embedding_model,
            )
            await store_reflection(
                reflection=reflection,
                store=store,
                embed_fn=embed_fn,
            )
        except Exception as e:
            logger.warning(f"Reflection failed for {pos.pair}: {e}")


async def trading_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
) -> None:
    """取引サイクルの全5フェーズを実行する。"""
    run_start = datetime.now(ZoneInfo(config.schedule.timezone))
    logger.info(f"=== Trading cycle started: {run_start.strftime('%Y-%m-%d %H:%M %Z')} ===")

    broker = create_broker(config.trading.trading_mode)
    notifier = create_notifier(config.notifier.notifier)
    llm_price = create_llm_client(config, "price_analysis")
    llm_reflect = create_llm_client(config, "reflection")
    logger.info(
        f"[TRADE] mode={config.trading.trading_mode} broker={type(broker).__name__} "
        f"notifier={type(notifier).__name__} "
        f"price={type(llm_price).__name__}({llm_price.model_name}) "
        f"reflect={type(llm_reflect).__name__}({llm_reflect.model_name})"
    )

    if not is_market_open(run_start):
        logger.info(f"Market {market_status_label(run_start)}. Skipping trading cycle.")
        return

    # Phase 1: SL/TP確認・クローズ
    account = position_mgr.get_account_state()
    closed_this_run = []
    if account.open_positions:
        current_prices = {}
        for pos in account.open_positions:
            try:
                current_prices[pos.pair] = fetch_current_price(pos.pair)
            except Exception as e:
                logger.warning(f"Could not fetch price for {pos.pair}: {e}")
        closed_this_run = broker.check_and_close_positions(
            account.open_positions, current_prices, position_mgr
        )
        if config.notifier.notify_on_order_close:
            account_after_close = position_mgr.get_account_state()
            for closed in closed_this_run:
                await notifier.notify_order_closed(OrderClosedEvent(
                    pair=closed.pair,
                    direction=closed.direction,
                    entry_price=closed.entry_price,
                    close_price=closed.close_price or 0.0,
                    realized_pnl=closed.realized_pnl or 0.0,
                    close_reason=closed.close_reason or "manual",
                    balance=account_after_close.balance,
                ))

    # Phase 2: 振り返り生成
    await _generate_cycle_reflections(config, position_mgr, store, llm_reflect)

    # Phase 3: 各ペアを並列分析（蓄積済みスナップショットを集約）
    semaphore = asyncio.Semaphore(config.llm.ollama.max_concurrent)

    async def bounded(pair_cfg):
        async with semaphore:
            return await _process_pair(
                pair_cfg, config, position_mgr, store, price_store, analysis_store, llm_price
            )

    results = await asyncio.gather(
        *[bounded(p) for p in config.enabled_pairs],
        return_exceptions=True,
    )

    signals = [r for r in results if not isinstance(r, Exception)]
    errors  = [r for r in results if isinstance(r, Exception)]
    if errors:
        logger.warning(f"{len(errors)} pair(s) failed during analysis.")

    # Phase 4: シグナル実行
    executed_orders = []
    for sig in signals:
        if sig.action != "hold":
            order = broker.execute_signal(sig, position_mgr)
            if order:
                executed_orders.append(order)
                if config.notifier.notify_on_order_open:
                    await notifier.notify_order_opened(OrderOpenedEvent(
                        pair=sig.pair,
                        direction=sig.action,
                        entry_price=sig.entry_price,
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                        position_size=sig.position_size,
                        confidence=sig.confidence,
                        signal_reason=sig.signal_reason,
                    ))
            elif config.notifier.notify_on_signal_skipped:
                await notifier.notify_signal_skipped(SignalSkippedEvent(
                    pair=sig.pair,
                    action=sig.action,
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                ))

    # Phase 5: レポート
    account_state = position_mgr.get_account_state()
    print_run_summary(
        signals=signals,
        executed_orders=executed_orders,
        closed_this_run=closed_this_run,
        account_state=account_state,
        run_start=run_start.replace(tzinfo=None),
    )
    logger.info(f"=== Cycle complete. Balance: ${account_state.balance:.2f} ===")


def run_trading_cycle(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance)
    asyncio.run(trading_cycle(config, position_mgr, store, price_store, analysis_store))
