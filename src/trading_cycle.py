from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from functools import partial

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.analysis.price_analyzer import analyze_price_action, chat_with_context, load_user_notes
from src.analysis.reflector import generate_close_reflection, generate_reflection, store_reflection
from src.config import AppConfig, InstrumentConfig
from src.data.indicators import compute_indicators
from src.data.price_fetcher import fetch_current_price, fetch_ohlcv
from src.data.analysis_store import AnalysisStore, HoldDecisionStore
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.embedder import embed_text
from src.rag.prompt_formatter import format_macro_context_for_prompt, format_news_for_prompt, format_reflections_for_prompt
from src.rag.vector_store import VectorStore
from src.notifications.notifier import (
    OrderClosedEvent,
    OrderOpenedEvent,
    SignalSkippedEvent,
    create_notifier,
)
from src.reporting.reporter import print_news_summary, print_run_summary, print_tech_summary
from src.signals.signal_combiner import combine_signals
from src.trading.live_broker import create_broker
from src.trading.market_hours import is_market_open, market_status_label
from src.trading.position_manager import PositionManager
from src.trading.position_reviewer import review_open_positions

logger = logging.getLogger(__name__)


async def _build_rag_context(
    pair_cfg, config: AppConfig, store: VectorStore
) -> tuple[str, str]:
    """RAGからニュースと振り返りコンテキストを取得する。"""
    news_entries = store.get_recent_category_news(
        categories=pair_cfg.news_categories,
        lookback_hours=config.rag.news_lookback_hours,
    )
    news_ctx = format_news_for_prompt(news_entries)

    reflections = store.get_recent_reflections(
        pair=pair_cfg.symbol,
        limit=config.rag.reflection_lookback_count,
    )
    refl_ctx = format_reflections_for_prompt(reflections)

    return news_ctx, refl_ctx


def _build_macro_context(config: AppConfig, analysis_store: AnalysisStore) -> str:
    """watch_only 銘柄の直近スナップショットからマクロコンテキストを構築する。"""
    watch_only = config.watch_only_instruments
    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    return format_macro_context_for_prompt(macro_snapshots, watch_only)


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
    戻り値: (TradeSignal, macro_ctx_str)
    """
    try:
        price_data = fetch_ohlcv(
            pair_cfg.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )

        macro_ctx = _build_macro_context(config, analysis_store)

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
            _, summary = compute_indicators(
                price_data.df,
                indicator_cfg=config.analysis.indicators,
                pattern_cfg=config.analysis.chart_patterns,
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
                macro_context=macro_ctx,
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
        return signal, macro_ctx
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
            pair_cfg = next((p for p in config.tradeable_instruments if p.symbol == pos.pair), None)
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
                user_notes=load_user_notes(config.user_notes_path, "reflect"),
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


async def _review_hold_decisions(
    config: AppConfig,
    hold_store: HoldDecisionStore,
    store: VectorStore,
) -> None:
    """前回HOLDした判断を検証してRAGに蓄積する（LLM不使用）。"""
    from src.analysis.forecaster import build_hold_review

    unreviewed = hold_store.get_unreviewed()
    if not unreviewed:
        return

    logger.info(f"[HOLD REVIEW] Reviewing {len(unreviewed)} hold decision(s)")
    embed_fn = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )

    for hold in unreviewed:
        try:
            current_price = fetch_current_price(hold.pair)
            review_text, lesson, worth_storing = build_hold_review(
                pair=hold.pair,
                hold=hold,
                current_price=current_price,
                review_ts=datetime.now(),
                significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
            )
            logger.info(f"[HOLD REVIEW] {hold.pair}: {review_text}")

            if worth_storing:
                embedding = await embed_fn(review_text)
                store.upsert_reflection(
                    entry_id=f"hold_{hold.pair}_{hold.id}",
                    text=review_text,
                    embedding=embedding,
                    pair=hold.pair,
                    cycle_time=datetime.now(),
                    action=hold.predicted_direction,
                    outcome_summary=review_text,
                    lesson=lesson,
                )
                logger.info(f"[HOLD REVIEW] {hold.pair}: stored to RAG")

            hold_store.mark_reviewed(hold.id)
        except Exception as e:
            logger.warning(f"[HOLD REVIEW] {hold.pair}: error — {e}")

    hold_store.prune_old()


async def trading_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    hold_store: HoldDecisionStore,
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

    # Phase 1.5: 決済トレードの確定結果ベース振り返りをRAGに保存
    if closed_this_run:
        embed_fn = partial(
            embed_text,
            ollama_base_url=config.llm.ollama.base_url,
            model=config.rag.embedding_model,
        )
        for closed_order in closed_this_run:
            pair_cfg = next(
                (p for p in config.tradeable_instruments if p.symbol == closed_order.pair),
                None,
            )
            if pair_cfg is None:
                continue
            try:
                reflection = await generate_close_reflection(
                    pair_cfg=pair_cfg,
                    order=closed_order,
                    llm=llm_reflect,
                    temperature=config.llm.reflection.temperature,
                    user_notes=load_user_notes(config.user_notes_path, "reflect"),
                )
                await store_reflection(
                    reflection=reflection,
                    store=store,
                    embed_fn=embed_fn,
                    close_reason=closed_order.close_reason,
                )
            except Exception as e:
                logger.warning(f"[REFLECT/CLOSE] Failed for {closed_order.pair}: {e}")

    # Phase 2: 振り返り生成
    await _generate_cycle_reflections(config, position_mgr, store, llm_reflect)

    # Phase 2.5: HOLD判断レビュー（前回HOLDの方向性を事実で検証）
    await _review_hold_decisions(config, hold_store, store)

    # Phase 3: 各ペアを並列分析（蓄積済みスナップショットを集約）
    semaphore = asyncio.Semaphore(config.llm.ollama.max_concurrent)

    async def bounded(pair_cfg):
        async with semaphore:
            return await _process_pair(
                pair_cfg, config, position_mgr, store, price_store, analysis_store, llm_price
            )

    results = await asyncio.gather(
        *[bounded(p) for p in config.tradeable_instruments],
        return_exceptions=True,
    )

    signals_with_macro = [r for r in results if not isinstance(r, Exception)]
    errors             = [r for r in results if isinstance(r, Exception)]
    signals    = [s for s, _ in signals_with_macro]
    macro_ctxs = {s.pair: m for s, m in signals_with_macro}
    if errors:
        logger.warning(f"{len(errors)} pair(s) failed during analysis.")

    # Phase 4a: ポジション再評価（Layer 1〜3）
    reviewed_closed = []
    if config.trading.position_review_enabled:
        account_for_review = position_mgr.get_account_state()
        if account_for_review.open_positions:
            signals_by_pair = {s.pair: s for s in signals}
            review_prices = {}
            for pos in account_for_review.open_positions:
                try:
                    review_prices[pos.pair] = fetch_current_price(pos.pair)
                except Exception as e:
                    logger.warning(f"[REVIEW] Could not fetch price for {pos.pair}: {e}")

            decisions = review_open_positions(
                open_positions=account_for_review.open_positions,
                signals_by_pair=signals_by_pair,
                current_prices=review_prices,
                reversal_confidence_min=config.trading.reversal_confidence_min,
                reversal_score_threshold=config.trading.reversal_score_threshold,
                max_holding_days=config.trading.max_holding_days,
                timeout_min_progress_pct=config.trading.timeout_min_progress_pct,
                profit_lock_min_progress_pct=config.trading.profit_lock_min_progress_pct,
                profit_lock_score_floor=config.trading.profit_lock_score_floor,
            )

            for decision in decisions:
                price = review_prices.get(decision.pair)
                if price is None:
                    continue
                closed_order = position_mgr.close_position(
                    decision.order_id, price, decision.close_reason,
                )
                if closed_order:
                    reviewed_closed.append(closed_order)
                    logger.info(
                        f"[REVIEW] {decision.pair}: closed ({decision.close_reason}) — "
                        f"{decision.detail}"
                    )
                    if config.notifier.notify_on_order_close:
                        account_after = position_mgr.get_account_state()
                        await notifier.notify_order_closed(OrderClosedEvent(
                            pair=closed_order.pair,
                            direction=closed_order.direction,
                            entry_price=closed_order.entry_price,
                            close_price=price,
                            realized_pnl=closed_order.realized_pnl or 0.0,
                            close_reason=decision.close_reason,
                            balance=account_after.balance,
                        ))

            # Phase 4a 決済分の振り返りをRAGに保存
            if reviewed_closed:
                embed_fn = partial(
                    embed_text,
                    ollama_base_url=config.llm.ollama.base_url,
                    model=config.rag.embedding_model,
                )
                for closed_order in reviewed_closed:
                    pair_cfg = next(
                        (p for p in config.tradeable_instruments if p.symbol == closed_order.pair),
                        None,
                    )
                    if pair_cfg is None:
                        continue
                    try:
                        reflection = await generate_close_reflection(
                            pair_cfg=pair_cfg,
                            order=closed_order,
                            llm=llm_reflect,
                            temperature=config.llm.reflection.temperature,
                            user_notes=load_user_notes(config.user_notes_path, "reflect"),
                        )
                        await store_reflection(
                            reflection=reflection,
                            store=store,
                            embed_fn=embed_fn,
                            close_reason=closed_order.close_reason,
                        )
                    except Exception as e:
                        logger.warning(f"[REFLECT/REVIEW] Failed for {closed_order.pair}: {e}")

    # Phase 4b: 新規シグナル実行
    executed_orders = []
    for sig in signals:
        if sig.action != "hold":
            order = broker.execute_signal(sig, position_mgr, macro_context=macro_ctxs.get(sig.pair, ""))
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
                        detail_reason=sig.detail_reason,
                    ))
            elif config.notifier.notify_on_signal_skipped:
                await notifier.notify_signal_skipped(SignalSkippedEvent(
                    pair=sig.pair,
                    action=sig.action,
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                    detail_reason=sig.detail_reason,
                ))
        else:
            # sig.action == "hold": シグナル弱く見送り → 次サイクルで結果を検証
            if config.notifier.notify_on_signal_skipped:
                await notifier.notify_signal_skipped(SignalSkippedEvent(
                    pair=sig.pair,
                    action="hold",
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                    detail_reason=sig.detail_reason,
                    predicted_direction=sig.predicted_direction,
                ))
            hold_store.save_hold(sig.pair, sig)

    # Phase 5: レポート
    all_closed = closed_this_run + reviewed_closed
    account_state = position_mgr.get_account_state()
    print_run_summary(
        signals=signals,
        executed_orders=executed_orders,
        closed_this_run=all_closed,
        account_state=account_state,
        run_start=run_start.replace(tzinfo=None),
    )
    logger.info(f"=== Cycle complete. Balance: ${account_state.balance:.2f} ===")


def run_trading_cycle(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    hold_store: HoldDecisionStore,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="TradingCycle")
    asyncio.run(trading_cycle(config, position_mgr, store, price_store, analysis_store, hold_store))


async def exit_check_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """出口専用軽量サイクル（毎時:00実行）。

    LLM不使用。キャッシュ済みスナップショットを集約して
    SL/TP確認とポジション再評価（Layer1-3）のみ実行する。
    新規発注・振り返り生成はスキップ。
    """
    run_start = datetime.now(ZoneInfo(config.schedule.timezone))

    if not is_market_open(run_start):
        return

    account = position_mgr.get_account_state()
    if not account.open_positions:
        return

    logger.info(
        f"=== Exit check: {run_start.strftime('%H:%M %Z')} "
        f"({len(account.open_positions)} open positions) ==="
    )

    broker = create_broker(config.trading.trading_mode)
    notifier = create_notifier(config.notifier.notifier)

    # Phase 1: SL/TP確認
    current_prices: dict[str, float] = {}
    for pos in account.open_positions:
        try:
            current_prices[pos.pair] = fetch_current_price(pos.pair)
        except Exception as e:
            logger.warning(f"[EXIT] Could not fetch price for {pos.pair}: {e}")

    closed_this_run = broker.check_and_close_positions(
        account.open_positions, current_prices, position_mgr
    )
    if config.notifier.notify_on_order_close:
        account_after = position_mgr.get_account_state()
        for closed in closed_this_run:
            await notifier.notify_order_closed(OrderClosedEvent(
                pair=closed.pair,
                direction=closed.direction,
                entry_price=closed.entry_price,
                close_price=closed.close_price or 0.0,
                realized_pnl=closed.realized_pnl or 0.0,
                close_reason=closed.close_reason or "manual",
                balance=account_after.balance,
            ))

    # Phase 4a: ポジション再評価（キャッシュ集約のみ、LLM不使用）
    if not config.trading.position_review_enabled:
        return

    account_for_review = position_mgr.get_account_state()
    if not account_for_review.open_positions:
        return

    open_pairs = {pos.pair for pos in account_for_review.open_positions}
    relevant_cfgs = [p for p in config.tradeable_instruments if p.symbol in open_pairs]

    sig_results = await asyncio.gather(
        *[_summarize_pair(p, config, position_mgr, store, analysis_store) for p in relevant_cfgs],
        return_exceptions=True,
    )
    signals_by_pair = {
        s.pair: s for s in sig_results
        if s is not None and not isinstance(s, Exception)
    }

    decisions = review_open_positions(
        open_positions=account_for_review.open_positions,
        signals_by_pair=signals_by_pair,
        current_prices=current_prices,
        reversal_confidence_min=config.trading.reversal_confidence_min,
        reversal_score_threshold=config.trading.reversal_score_threshold,
        max_holding_days=config.trading.max_holding_days,
        timeout_min_progress_pct=config.trading.timeout_min_progress_pct,
        profit_lock_min_progress_pct=config.trading.profit_lock_min_progress_pct,
        profit_lock_score_floor=config.trading.profit_lock_score_floor,
    )

    for decision in decisions:
        price = current_prices.get(decision.pair)
        if price is None:
            continue
        closed_order = position_mgr.close_position(
            decision.order_id, price, decision.close_reason,
        )
        if closed_order:
            _LAYER_LABEL = {"reversal": "L1", "timeout": "L2", "profit_lock": "L3"}
            layer = _LAYER_LABEL.get(decision.close_reason, decision.close_reason)
            logger.info(
                f"[EXIT] {decision.pair}: closed {layer}({decision.close_reason}) — {decision.detail}"
            )
            if config.notifier.notify_on_order_close:
                account_after = position_mgr.get_account_state()
                await notifier.notify_order_closed(OrderClosedEvent(
                    pair=closed_order.pair,
                    direction=closed_order.direction,
                    entry_price=closed_order.entry_price,
                    close_price=price,
                    realized_pnl=closed_order.realized_pnl or 0.0,
                    close_reason=decision.close_reason,
                    balance=account_after.balance,
                ))


def run_exit_check_cycle(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="ExitCheck")
    asyncio.run(exit_check_cycle(config, position_mgr, store, analysis_store))


async def forecast_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    forecast_store,
) -> None:
    """予測サイクル（設定間隔ごと実行）。

    LLM不使用。ノイズ対策 A+B+C+D を適用:
      A: ATR proxy による有意性フィルター（小動きはスキップ）
      B: 8h検証ウィンドウ（呼び出し間隔で制御）
      C: 高確信度シグナルのみ予測生成
      D: 事実文字列として RAG に蓄積（LLM解釈なし）
    """
    from src.analysis.forecaster import build_forecast_review_summary

    now = datetime.now(ZoneInfo(config.schedule.timezone))

    if not is_market_open(now):
        return

    logger.info(f"=== Forecast cycle: {now.strftime('%H:%M %Z')} ===")

    embed_fn = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )

    for pair_cfg in config.tradeable_instruments:
        try:
            # Phase 1: 直近24hの全予測を毎サイクル更新検証（D）
            recent_forecasts = forecast_store.get_recent_forecasts(pair_cfg.symbol, hours=24)
            if recent_forecasts:
                try:
                    current_price = fetch_current_price(pair_cfg.symbol)
                except Exception as e:
                    logger.warning(f"[FORECAST] {pair_cfg.symbol}: price fetch failed — {e}")
                    continue

                review_ts = datetime.now()

                # 各予測のdeltaを更新
                for fc in recent_forecasts:
                    delta = current_price - fc.current_price
                    forecast_store.update_review(fc.id, delta)

                # 24h集計サマリーをRAGに上書きupsert（D）
                summary_text, lesson, has_significant = build_forecast_review_summary(
                    pair=pair_cfg.symbol,
                    forecasts=recent_forecasts,
                    current_price=current_price,
                    review_ts=review_ts,
                    significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
                )
                logger.info(f"[FORECAST] {pair_cfg.symbol}: {summary_text}")

                if has_significant:
                    embedding = await embed_fn(summary_text)
                    store.upsert_reflection(
                        entry_id=f"forecast_summary_{pair_cfg.symbol}_{now.strftime('%Y-%m-%d')}",
                        text=summary_text,
                        embedding=embedding,
                        pair=pair_cfg.symbol,
                        cycle_time=review_ts,
                        action=recent_forecasts[-1].predicted_direction,
                        outcome_summary=summary_text,
                        lesson=lesson,
                    )

            # Phase 2: 新規予測生成（C: スコア閾値チェック、LLM不使用）
            signal = await _summarize_pair(pair_cfg, config, position_mgr, store, analysis_store)
            if signal is None:
                logger.info(f"[FORECAST] {pair_cfg.symbol}: skip — no_snapshot")
                continue

            # C: deadband を超えた高確信度シグナルのみ予測対象
            if abs(signal.combined_score) < config.analysis.forecast_min_combined_score:
                forecast_store.save_forecast_skip(pair_cfg.symbol, signal)
                continue

            macro_ctx = _build_macro_context(config, analysis_store)
            forecast_store.save_forecast(pair_cfg.symbol, signal, macro_context=macro_ctx)

        except Exception as e:
            logger.warning(f"[FORECAST] {pair_cfg.symbol}: error — {e}", exc_info=True)

    forecast_store.prune_old()
    logger.info("=== Forecast cycle complete ===")


def run_forecast_view(config: AppConfig, forecast_store, pair_filter: str | None = None) -> None:
    """CLIから呼び出す: 直近24hの予測データをテーブル表示する（新規取得なし）。"""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    console = Console()

    targets = [p for p in config.tradeable_instruments if pair_filter is None or p.symbol == pair_filter]
    if not targets:
        console.print(f"[red]対象ペアが見つかりません: {pair_filter}[/red]")
        return

    console.print(f"\n[bold cyan]=== Forecast Data (直近24h) ===[/bold cyan]")

    for inst in targets:
        records = forecast_store.get_recent_all(inst.symbol, hours=24)
        console.print(f"\n[bold]{inst.display_name}[/bold]")
        if not records:
            console.print("  [dim]データなし[/dim]")
            continue

        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        tbl.add_column("生成時刻", style="dim")
        tbl.add_column("方向", justify="center")
        tbl.add_column("score", justify="right")
        tbl.add_column("conf", justify="right")
        tbl.add_column("最終検証時刻", style="dim")
        tbl.add_column("delta", justify="right")

        for r in records:
            direction_color = "green" if r.predicted_direction == "bullish" else ("red" if r.predicted_direction == "bearish" else "dim")
            score_color = "green" if r.combined_score > 0 else ("red" if r.combined_score < 0 else "dim")

            if r.reviewed == 3:
                # skipレコード
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    "[dim]–[/dim]",
                    "[dim]skip(score不足)[/dim]",
                )
            elif r.reviewed == 0 or r.latest_review_ts is None:
                # 未検証
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    "[dim]未検証[/dim]",
                    "[dim]–[/dim]",
                )
            else:
                # 検証済: deltaの色はbullish+正 or bearish+負なら緑、それ以外は赤
                delta = r.latest_price_delta or 0.0
                direction_match = (
                    (r.predicted_direction == "bullish" and delta > 0) or
                    (r.predicted_direction == "bearish" and delta < 0)
                )
                delta_color = "green" if direction_match else "red"
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    r.latest_review_ts.strftime("%m-%d %H:%M"),
                    f"[{delta_color}]{delta:+.5f}[/{delta_color}]",
                )

        console.print(tbl)

        forecast_records = [r for r in records if r.reviewed != 3]
        reviewed_records = [r for r in forecast_records if r.reviewed == 1]
        unreviewed_records = [r for r in forecast_records if r.reviewed == 0]
        skipped = [r for r in records if r.reviewed == 3]

        if reviewed_records:
            deltas = [r.latest_price_delta for r in reviewed_records if r.latest_price_delta is not None]
            avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
            direction_counts: dict[str, int] = {}
            for r in reviewed_records:
                direction_counts[r.predicted_direction] = direction_counts.get(r.predicted_direction, 0) + 1
            dir_summary = " ".join(f"{d}×{c}" for d, c in direction_counts.items())
            console.print(
                f"  avg_delta=[bold]{avg_delta:+.5f}[/bold] | {dir_summary} | "
                f"未検証: {len(unreviewed_records)}件 | skip: {len(skipped)}件"
            )
        else:
            console.print(f"  未検証: {len(unreviewed_records)}件 | skip: {len(skipped)}件")


def run_forecast_cycle(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    forecast_store,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="ForecastCycle")
    asyncio.run(forecast_cycle(config, position_mgr, store, analysis_store, forecast_store))


async def _summarize_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
):
    """保存済み分析スナップショットとニュースからシグナルを算出する（新規取得なし）。"""
    try:
        price = analysis_store.aggregate(
            pair_cfg.symbol,
            hours=config.rag.analysis_lookback_hours,
        )
        if price is None:
            logger.warning(f"{pair_cfg.display_name}: 保存済みスナップショットなし、スキップ")
            return None

        news = aggregate_news_sentiment(pair_cfg, store, config)
        current_price = fetch_current_price(pair_cfg.symbol)

        account = position_mgr.get_account_state()
        return combine_signals(
            news=news,
            price=price,
            current_price=current_price,
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
    except Exception as e:
        logger.error(f"Failed to summarize {pair_cfg.display_name}: {e}", exc_info=True)
        return None


async def _analysis_summary(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """保存済みの最新分析結果を集約して総合分析サマリーを表示する（新規取得なし）。"""
    run_start = datetime.now(ZoneInfo(config.schedule.timezone))
    logger.info(f"=== Analysis summary: {run_start.strftime('%Y-%m-%d %H:%M %Z')} ===")

    results = await asyncio.gather(
        *[_summarize_pair(p, config, position_mgr, store, analysis_store)
          for p in config.tradeable_instruments],
        return_exceptions=True,
    )

    signals = [r for r in results if r is not None and not isinstance(r, Exception)]
    if not signals:
        logger.warning("表示できるシグナルがありません。先に run tech でテクニカル分析を実行してください。")

    account_state = position_mgr.get_account_state()
    print_run_summary(
        signals=signals,
        executed_orders=[],
        closed_this_run=[],
        account_state=account_state,
        run_start=run_start.replace(tzinfo=None),
    )


def run_news_view(config: AppConfig, store: VectorStore) -> None:
    """CLIから呼び出す: 保存済みニュースセンチメントを表示する（新規取得なし）。"""
    entries_by_category = {
        cat: store.get_recent_category_news([cat], lookback_hours=config.rag.news_lookback_hours)
        for cat in ("fx", "global", "japan")
    }
    print_news_summary(entries_by_category, config.rag.news_lookback_hours)


def run_tech_view(config: AppConfig, analysis_store: AnalysisStore) -> None:
    """CLIから呼び出す: 保存済みテクニカルスナップショットを表示する（新規取得なし）。"""
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    snapshots_by_symbol = {
        inst.symbol: analysis_store.get_recent_snapshots(
            inst.symbol, hours=config.rag.analysis_lookback_hours
        )
        for inst in all_instruments
    }
    display_names = {inst.symbol: inst.display_name for inst in all_instruments}
    print_tech_summary(snapshots_by_symbol, display_names, config.rag.analysis_lookback_hours)


def run_analysis_summary(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """CLIから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="AnalysisSummary")
    asyncio.run(_analysis_summary(config, position_mgr, store, analysis_store))


def _build_ask_context(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    position_mgr: PositionManager,
) -> str:
    """askコマンド用コンテキスト文字列を構築する。"""
    lines: list[str] = []

    # テクニカルスナップショット（全銘柄）
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    lines.append("=== Technical Snapshots ===")
    for inst in all_instruments:
        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=config.rag.analysis_lookback_hours)
        if snaps:
            s = snaps[0]
            lines.append(
                f"{inst.display_name}: bias={s.bias_score:+.2f} conf={s.confidence:.2f} "
                f"dir={s.direction_bias} RR={s.risk_reward_ratio:.1f} | {s.reasoning_summary}"
            )
        else:
            lines.append(f"{inst.display_name}: no snapshot")

    # ニュースコンテキスト（FX・global・japan）
    lines.append("\n=== News Context ===")
    for cat in ("fx", "global", "japan"):
        entries = store.get_recent_category_news([cat], lookback_hours=config.rag.news_lookback_hours)
        if entries:
            for e in entries[:3]:
                meta = e.get("metadata", {})
                summary = meta.get("summary") or e.get("text", "")[:80]
                score = meta.get("sentiment_score", 0.0)
                lines.append(f"[{cat}] {summary} (sentiment={score:+.2f})")

    # オープンポジション
    account = position_mgr.get_account_state()
    lines.append("\n=== Open Positions ===")
    if account.open_positions:
        for pos in account.open_positions:
            lines.append(
                f"{pos.pair} {pos.direction.upper()} entry={pos.entry_price:.5f} "
                f"SL={pos.stop_loss:.5f} TP={pos.take_profit:.5f}"
            )
    else:
        lines.append("No open positions.")

    return "\n".join(lines)


async def _run_ask(
    user_message: str,
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> str:
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="Ask")
    context = _build_ask_context(config, store, analysis_store, position_mgr)
    llm = create_llm_client(config, "price_analysis")
    return await chat_with_context(
        user_message=user_message,
        context=context,
        llm=llm,
        temperature=config.llm.price_analysis.temperature,
    )


def run_ask(
    user_message: str,
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> str:
    """CLIから呼び出す同期ラッパー。"""
    return asyncio.run(_run_ask(user_message, config, store, analysis_store))
