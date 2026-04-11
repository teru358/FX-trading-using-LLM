from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from functools import partial

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.analysis.price_analyzer import analyze_price_action, load_user_notes
from src.analysis.reflector import generate_close_reflection, generate_reflection, store_reflection
from src.config import AppConfig, InstrumentConfig
from src.data.indicators import compute_indicators
from src.data.price_fetcher import fetch_current_price, fetch_ohlcv
from src.data.price_provider import PriceProvider
from src.data.analysis_store import AnalysisStore, HoldDecisionStore
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.directional_writer import (
    record_forecast_entry,
    record_forecast_review,
    record_hold_review,
    record_trade_complete,
    record_trade_entry,
)
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
from src.signals.rag_adjustment import RagAdjustmentConfig, compute_rag_adjustment
from src.signals.signal_combiner import combine_signals
from src.trading.atr_calculator import calculate_sl_tp
from src.trading.entry_context_builder import build_entry_context
from src.trading.live_broker import create_broker
from src.trading.market_hours import is_market_open, market_status_label
from src.trading.position_manager import PositionManager
from src.trading.position_reviewer import review_open_positions
from src.persistence.adaptive_params_store import AdaptiveParamsStore

logger = logging.getLogger(__name__)


def _get_price(symbol: str, price_provider: PriceProvider | None) -> float:
    """price_provider経由で現在価格を取得する。Noneの場合は直接呼び出しにフォールバック。"""
    if price_provider:
        return price_provider.get_current_price(symbol).price
    return fetch_current_price(symbol).price


def _get_ohlcv(symbol: str, period: str, interval: str, price_store, price_provider: PriceProvider | None):
    """price_provider経由でOHLCVを取得する。Noneの場合は直接呼び出しにフォールバック。"""
    if price_provider:
        return price_provider.get_ohlcv(symbol, period, interval, price_store)
    return fetch_ohlcv(symbol, period, interval, price_store)


def _compute_atr_from_price_data(price_data) -> float | None:
    """price_data から ATR(14) を計算する。データ不足や例外時は None。"""
    if not price_data or len(price_data.df) < 14:
        return None
    try:
        import pandas_ta as pta
        atr_s = pta.atr(
            price_data.df["High"], price_data.df["Low"], price_data.df["Close"], length=14,
        )
        if atr_s is None or atr_s.empty:
            return None
        return float(atr_s.iloc[-1])
    except Exception:
        return None


def _fetch_and_compute_atr(symbol: str, config: AppConfig, price_store) -> float | None:
    """price_store から OHLCV を取得し ATR(14) を返す。失敗時は None。"""
    if price_store is None:
        return None
    try:
        from src.data.price_fetcher import fetch_ohlcv as _fetch_ohlcv
        price_data = _fetch_ohlcv(
            symbol,
            f"{config.trading.lookback_days}d",
            config.trading.ohlcv_interval,
            price_store=price_store,
        )
        return _compute_atr_from_price_data(price_data)
    except Exception:
        return None


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

    # 経済指標分析レポートをreflection_contextに付加
    if config.economic_calendar.enabled:
        try:
            currencies = [
                c for c in (pair_cfg.base_currency, pair_cfg.quote_currency) if c
            ]
            econ_reports = store.get_recent_econ_analyses(
                currencies=currencies,
                lookback_minutes=config.economic_calendar.post_event_window_min,
                limit=3,
            )
            if econ_reports:
                lines = ["=== 直近の経済指標発表影響分析 ==="]
                for r in econ_reports:
                    meta = r["metadata"]
                    lines.append(
                        f"[{meta.get('event_time', '')}] {meta.get('title', '')} "
                        f"({meta.get('currency', '')}, surprise={meta.get('surprise', '')})"
                    )
                    lines.append(r["text"][:400])
                econ_ctx = "\n".join(lines)
                refl_ctx = f"{refl_ctx}\n\n{econ_ctx}" if refl_ctx else econ_ctx
                logger.info(
                    f"[ECON] Injected {len(econ_reports)} econ analyses into {pair_cfg.display_name} context"
                )
        except Exception as e:
            logger.debug(f"[ECON] RAG injection failed: {e}")

    return news_ctx, refl_ctx


def _build_macro_context(config: AppConfig, analysis_store: AnalysisStore) -> str:
    """watch_only 銘柄の直近スナップショットからマクロコンテキストを構築する。"""
    watch_only = config.watch_only_instruments
    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    return format_macro_context_for_prompt(
        macro_snapshots, watch_only,
        realtime_provider=config.price_provider.realtime_provider,
    )


async def _process_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
    price_provider: PriceProvider | None = None,
):
    """1ペアの分析→シグナル生成。

    テクニカル分析は収集ジョブで蓄積済みのスナップショットを集約して使用する。
    スナップショットが存在しない場合（初回起動直後など）はOllama即時分析にフォールバック。
    戻り値: (TradeSignal, macro_ctx_str)
    """
    try:
        price_data = _get_ohlcv(
            pair_cfg.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
            price_provider=price_provider,
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
            from src.signals.technical_scorer import compute_technical_score
            tech_score = compute_technical_score(summary)
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
                tech_score=tech_score,
            )

        news = aggregate_news_sentiment(pair_cfg, store, config)

        # TradingView テクニカルサマリー取得 (オプション・矛盾検出用)
        tv_summary = None
        if config.trading.tv_summary_enabled:
            from src.analysis.tv_summary import get_tv_summary
            tv_summary = get_tv_summary(pair_cfg.symbol, interval=config.trading.ohlcv_interval)
            if tv_summary:
                logger.info(
                    f"[TV-TA] {pair_cfg.display_name}: {tv_summary.recommendation} "
                    f"(buy={tv_summary.buy} sell={tv_summary.sell} neutral={tv_summary.neutral})"
                )

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
            tv_summary=tv_summary,
            tv_conflict_dampen=config.trading.tv_conflict_dampen,
        )
        return signal, macro_ctx
    except Exception as e:
        logger.error(f"Failed to process {pair_cfg.display_name}: {e}", exc_info=True)
        return e


async def _generate_cycle_reflections(
    config: AppConfig, position_mgr: PositionManager, store: VectorStore, llm: LLMClient,
    price_provider: PriceProvider | None = None,
) -> None:
    """オープンポジションに対して振り返りを生成・RAGに蓄積する。"""
    for pos in position_mgr.get_account_state().open_positions:
        try:
            current_price = _get_price(pos.pair, price_provider)
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
            # レガシーfx_reflectionsへの書き込みは停止（方向別RAGに移行済み）
            # await store_reflection(reflection=reflection, store=store, embed_fn=embed_fn)
        except Exception as e:
            logger.warning(f"Reflection failed for {pos.pair}: {e}")


def _apply_atr_sltp_to_signal(
    sig,
    config: AppConfig,
    position_mgr: PositionManager,
    price_store: PriceStore,
    adaptive_store: "AdaptiveParamsStore",
    price_provider: PriceProvider | None = None,
):
    """ATR(14) ベースで SL/TP を再計算し、シグナルへ反映する。

    成功時:
      - sig.stop_loss / sig.take_profit を計算結果で上書き
      - sig.position_size を新しい SL 距離でリスク計算し直して上書き
      - SLTPResult を返す (session 記録に使う)

    OHLCV 取得失敗 / ATR 不足 / その他例外時は None を返し、シグナルは無変更。
    """
    atr_params = adaptive_store.get_params(sig.pair)
    try:
        price_data = _get_ohlcv(
            sig.pair, f"{config.trading.lookback_days}d",
            config.trading.ohlcv_interval, price_store,
            price_provider,
        )
        atr_val = _compute_atr_from_price_data(price_data)
        if not atr_val or atr_val <= 0:
            return None

        sltp_result = calculate_sl_tp(
            direction=sig.action,
            entry_price=sig.entry_price,
            atr_value=atr_val,
            sl_atr_mult=atr_params["sl_atr_mult"],
            tp_atr_mult=atr_params["tp_atr_mult"],
            llm_sl=sig.stop_loss,
            llm_tp=sig.take_profit,
            swing_highs=list(getattr(sig.price, "entry_zone", (0, 0))),
            swing_lows=[],
            key_support=getattr(sig.price, "key_support", None),
            key_resistance=getattr(sig.price, "key_resistance", None),
        )
        sig.stop_loss = sltp_result.computed_sl
        sig.take_profit = sltp_result.computed_tp

        from src.signals.signal_combiner import _calculate_position_size
        pair_cfg_for_size = next(
            (p for p in config.tradeable_instruments if p.symbol == sig.pair), None
        )
        if pair_cfg_for_size:
            sig.position_size = _calculate_position_size(
                balance=position_mgr.get_account_state().balance,
                risk_pct=config.trading.risk_per_trade,
                entry=sig.entry_price,
                stop_loss=sltp_result.computed_sl,
                pip_value=pair_cfg_for_size.pip_value,
                min_lot_size=config.trading.min_lot_size,
                lot_unit=config.trading.lot_unit,
            )
        return sltp_result
    except Exception as e:
        logger.warning(f"[ATR] {sig.pair}: ATR SL/TP calculation failed — {e}")
        return None


async def _finalize_closed_orders(
    closed_orders: list,
    config: AppConfig,
    store: VectorStore,
    embed_fn,
    llm_reflect: LLMClient,
    adaptive_store: "AdaptiveParamsStore",
    session_store,
    log_source: str,
) -> None:
    """決済済みオーダー群に対し、振り返り生成 → 適応パラメータ更新 → セッション終了 →
    directional RAG への complete upsert までを実行する。

    log_source は失敗時のログプレフィックス (例: '[REFLECT/CLOSE]', '[REFLECT/REVIEW]')。
    """
    if not closed_orders:
        return

    for closed_order in closed_orders:
        pair_cfg = next(
            (p for p in config.tradeable_instruments if p.symbol == closed_order.pair),
            None,
        )
        if pair_cfg is None:
            continue
        try:
            entry_analysis = ""
            sltp_comparison = ""
            param_history_text = ""
            if session_store:
                sess = session_store.get_session(closed_order.order_id)
                if sess:
                    entry_analysis = sess.analysis_summary or ""
                    if sess.atr_value and sess.computed_sl:
                        sltp_comparison = (
                            f"ATR(14)={sess.atr_value:.5f} sl_mult={sess.sl_atr_mult} tp_mult={sess.tp_atr_mult}\n"
                            f"computed: SL={sess.computed_sl:.5f} TP={sess.computed_tp:.5f}\n"
                            f"llm: SL={sess.llm_sl:.5f} TP={sess.llm_tp:.5f}\n"
                            f"Actual close: {(closed_order.close_price or closed_order.entry_price):.5f} ({closed_order.close_reason})"
                        )
                    history = adaptive_store.get_history(closed_order.pair, limit=3)
                    if history:
                        param_history_text = "\n".join(
                            f"[{h.get('updated_at', '?')}] sl={h.get('sl_atr_mult')} tp={h.get('tp_atr_mult')} reason={h.get('reason', '')}"
                            for h in history
                        )

            reflection = await generate_close_reflection(
                pair_cfg=pair_cfg,
                order=closed_order,
                llm=llm_reflect,
                temperature=config.llm.reflection.temperature,
                user_notes=load_user_notes(config.user_notes_path, "reflect"),
                entry_analysis=entry_analysis,
                sltp_comparison=sltp_comparison,
                param_history=param_history_text,
            )

            if reflection.atr_params_suggestion:
                suggestion = reflection.atr_params_suggestion
                new_params = {}
                if suggestion.get("sl_atr_mult") is not None:
                    new_params["sl_atr_mult"] = suggestion["sl_atr_mult"]
                if suggestion.get("tp_atr_mult") is not None:
                    new_params["tp_atr_mult"] = suggestion["tp_atr_mult"]
                if new_params:
                    try:
                        adaptive_store.update_params(
                            pair=closed_order.pair,
                            new_params=new_params,
                            reason=suggestion.get("reason", "LLM suggestion"),
                            trade_id=closed_order.order_id,
                        )
                    except Exception as e:
                        logger.warning(f"[ADAPTIVE] {closed_order.pair}: param update failed — {e}")

            if session_store:
                session_store.close_session(
                    session_id=closed_order.order_id,
                    closed_at=closed_order.closed_at or datetime.now(),
                    close_price=closed_order.close_price or closed_order.entry_price,
                    close_reason=closed_order.close_reason or "manual",
                    realized_pnl=closed_order.realized_pnl or 0.0,
                    reflection_text=reflection.full_text if reflection else "",
                )
                await record_trade_complete(
                    store, embed_fn, closed_order,
                    reflection.full_text if reflection else "",
                )
        except Exception as e:
            logger.warning(f"{log_source} Failed for {closed_order.pair}: {e}")


async def _review_hold_decisions(
    config: AppConfig,
    hold_store: HoldDecisionStore,
    store: VectorStore,
    price_provider: PriceProvider | None = None,
    price_store=None,
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
            current_price = _get_price(hold.pair, price_provider)

            hold_atr = _fetch_and_compute_atr(hold.pair, config, price_store)

            review_text, lesson, worth_storing = build_hold_review(
                pair=hold.pair,
                hold=hold,
                current_price=current_price,
                review_ts=datetime.now(),
                significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
                atr_value=hold_atr,
            )
            logger.info(f"[HOLD REVIEW] {hold.pair}: {review_text}")

            if worth_storing:
                await record_hold_review(store, embed_fn, hold, review_text, lesson)

            hold_store.mark_reviewed(hold.id)
        except Exception as e:
            logger.warning(f"[HOLD REVIEW] {hold.pair}: error — {e}")

    hold_store.prune_old()


async def _phase_close_sl_tp(
    config: AppConfig,
    position_mgr: PositionManager,
    broker,
    notifier,
    price_provider: PriceProvider | None,
) -> list:
    """Phase 1: SL/TP に到達した既存ポジションをチェックして決済する。"""
    account = position_mgr.get_account_state()
    if not account.open_positions:
        return []

    current_prices: dict[str, float] = {}
    for pos in account.open_positions:
        try:
            current_prices[pos.pair] = _get_price(pos.pair, price_provider)
        except Exception as e:
            logger.warning(f"Could not fetch price for {pos.pair}: {e}")

    closed_this_run = broker.check_and_close_positions(
        account.open_positions, current_prices, position_mgr,
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
                source="trading",
            ))
    return closed_this_run


async def _phase_analyze_pairs(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm_price: LLMClient,
    price_provider: PriceProvider | None,
) -> tuple[list, dict[str, str]]:
    """Phase 3: 全ペアを並列分析してシグナル + macro_ctx を生成する。"""
    semaphore = asyncio.Semaphore(config.llm.ollama.max_concurrent)

    async def bounded(pair_cfg):
        async with semaphore:
            return await _process_pair(
                pair_cfg, config, position_mgr, store, price_store, analysis_store, llm_price,
                price_provider=price_provider,
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
    return signals, macro_ctxs


async def _phase_review_open_positions(
    config: AppConfig,
    position_mgr: PositionManager,
    signals: list,
    notifier,
    price_provider: PriceProvider | None,
) -> list:
    """Phase 4a: position_review (Layer 1-3) を実行し決済済オーダーを返す。"""
    if not config.trading.position_review_enabled:
        return []

    account_for_review = position_mgr.get_account_state()
    if not account_for_review.open_positions:
        return []

    signals_by_pair = {s.pair: s for s in signals}
    review_prices: dict[str, float] = {}
    for pos in account_for_review.open_positions:
        try:
            review_prices[pos.pair] = _get_price(pos.pair, price_provider)
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

    reviewed_closed = []
    for decision in decisions:
        price = review_prices.get(decision.pair)
        if price is None:
            continue
        closed_order = position_mgr.close_position(
            decision.order_id, price, decision.close_reason,
        )
        if not closed_order:
            continue
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
                source="trading",
            ))
    return reviewed_closed


async def _adjust_signal_with_rag(
    sig,
    rag_cfg: RagAdjustmentConfig,
    store: VectorStore,
    embed_fn_adj,
    deadband: float,
) -> None:
    """方向別RAGの過去成績をもとにシグナルスコアを補正し、必要なら action も再判定する。"""
    if not (rag_cfg.enabled and sig.action != "hold"):
        return
    try:
        query_embedding = await embed_fn_adj(sig.detail_reason)
        same_dir = "bullish" if sig.combined_score > 0 else "bearish"
        opposite_dir = "bearish" if sig.combined_score > 0 else "bullish"
        same_hits = store.directional.query(
            query_embedding=query_embedding, direction=same_dir,
            top_k=rag_cfg.search_top_n, phase_filter="complete",
        )
        opposite_hits = store.directional.query(
            query_embedding=query_embedding, direction=opposite_dir,
            top_k=rag_cfg.search_top_n, phase_filter="complete",
        )
        adjustment = compute_rag_adjustment(
            combined_score=sig.combined_score,
            same_direction_hits=same_hits,
            opposite_direction_hits=opposite_hits,
            config=rag_cfg,
        )
    except Exception as e:
        logger.warning(f"[RAG ADJ] {sig.pair}: failed — {e}")
        return

    adjusted_score = sig.combined_score + adjustment
    if adjusted_score == sig.combined_score:
        return

    logger.info(f"[RAG ADJ] {sig.pair}: combined={sig.combined_score:+.3f} → adjusted={adjusted_score:+.3f}")
    if adjusted_score > deadband:
        sig.action = "buy"
    elif adjusted_score < -deadband:
        sig.action = "sell"
    else:
        sig.action = "hold"
    sig.combined_score = round(adjusted_score, 4)


async def _execute_one_signal(
    sig,
    macro_ctx: str,
    config: AppConfig,
    position_mgr: PositionManager,
    broker,
    notifier,
    store: VectorStore,
    price_store: PriceStore,
    session_store,
    adaptive_store: "AdaptiveParamsStore",
    embed_fn_adj,
    price_provider: PriceProvider | None,
):
    """1シグナルの発注処理 (ATR SL/TP → broker → session → RAG 記録) を実行する。"""
    sltp_result = _apply_atr_sltp_to_signal(
        sig, config, position_mgr, price_store, adaptive_store,
        price_provider=price_provider,
    )

    order = broker.execute_signal(sig, position_mgr, macro_context=macro_ctx)
    if not order:
        if config.notifier.notify_on_signal_skipped:
            await notifier.notify_signal_skipped(SignalSkippedEvent(
                pair=sig.pair,
                action=sig.action,
                confidence=sig.confidence,
                signal_reason=sig.signal_reason,
                detail_reason=sig.detail_reason,
                source="trading",
            ))
        return None

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
            source="trading",
        ))

    if session_store:
        direction = "bullish" if order.direction == "buy" else "bearish"
        entry_ctx = ""
        if sltp_result:
            entry_ctx = build_entry_context(
                combined_score=sig.combined_score,
                confidence=sig.confidence,
                action=sig.action,
                news_weight=config.trading.news_weight,
                price_weight=config.trading.price_weight,
                news=sig.news,
                price=sig.price,
                sltp=sltp_result,
                macro_context=macro_ctx,
            )
        session_store.create_session(
            session_id=order.order_id,
            pair=order.pair,
            direction=direction,
            entry_price=order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            position_size=order.position_size,
            signal_score=sig.combined_score,
            signal_confidence=sig.confidence,
            macro_context=macro_ctx,
            analysis_summary=entry_ctx or sig.detail_reason,
            opened_at=order.opened_at,
            atr_value=sltp_result.atr_value if sltp_result else None,
            sl_atr_mult=sltp_result.sl_atr_mult if sltp_result else None,
            tp_atr_mult=sltp_result.tp_atr_mult if sltp_result else None,
            computed_sl=sltp_result.computed_sl if sltp_result else None,
            computed_tp=sltp_result.computed_tp if sltp_result else None,
            llm_sl=sltp_result.llm_sl if sltp_result else None,
            llm_tp=sltp_result.llm_tp if sltp_result else None,
            key_support=sltp_result.key_support if sltp_result else None,
            key_resistance=sltp_result.key_resistance if sltp_result else None,
        )
        await record_trade_entry(store, embed_fn_adj, order, sig)
    return order


async def _phase_execute_signals(
    signals: list,
    macro_ctxs: dict[str, str],
    config: AppConfig,
    position_mgr: PositionManager,
    broker,
    notifier,
    store: VectorStore,
    price_store: PriceStore,
    hold_store: HoldDecisionStore,
    session_store,
    adaptive_store: "AdaptiveParamsStore",
    embed_fn_adj,
    price_provider: PriceProvider | None,
) -> list:
    """Phase 4b: シグナルにRAG補正を適用し、新規発注 or HOLD保存を実行する。"""
    rag_cfg = RagAdjustmentConfig(
        enabled=config.trading.rag_adjustment_enabled,
        max_adjustment=config.trading.rag_adjustment_max,
        min_hits=config.trading.rag_adjustment_min_hits,
        search_top_n=config.trading.rag_adjustment_search_top_n,
        same_direction_weight=config.trading.rag_adjustment_same_weight,
        opposite_direction_weight=config.trading.rag_adjustment_opposite_weight,
        trade_weight_multiplier=config.trading.rag_adjustment_trade_multiplier,
        forecast_weight_multiplier=config.trading.rag_adjustment_forecast_multiplier,
        hold_weight_multiplier=config.trading.rag_adjustment_hold_multiplier,
    )
    deadband = config.trading.signal_deadband

    executed_orders = []
    for sig in signals:
        await _adjust_signal_with_rag(sig, rag_cfg, store, embed_fn_adj, deadband)

        if sig.action != "hold":
            order = await _execute_one_signal(
                sig, macro_ctxs.get(sig.pair, ""),
                config, position_mgr, broker, notifier, store, price_store,
                session_store, adaptive_store, embed_fn_adj,
                price_provider=price_provider,
            )
            if order:
                executed_orders.append(order)
        else:
            if config.notifier.notify_on_signal_skipped:
                await notifier.notify_signal_skipped(SignalSkippedEvent(
                    pair=sig.pair,
                    action="hold",
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                    detail_reason=sig.detail_reason,
                    predicted_direction=sig.predicted_direction,
                    source="trading",
                ))
            hold_store.save_hold(sig.pair, sig)
    return executed_orders


async def _phase_render_tradingview(
    config: AppConfig,
    analysis_store: AnalysisStore,
    position_mgr: PositionManager,
) -> None:
    """TradingView チャートにシグナル + ポジションを反映する。"""
    if not config.tradingview.enabled:
        return
    try:
        from src.tradingview.cdp_client import CDPClient
        from src.tradingview.pine_injector import PineInjector
        from src.tradingview.tv_payload import build_tv_pine

        pine = build_tv_pine(config, analysis_store, position_mgr)
        if pine is None:
            logger.debug("[TV] No signals or positions to render")
            return

        tv_cdp = CDPClient(host=config.tradingview.cdp_host, port=config.tradingview.cdp_port)
        if not await tv_cdp.connect():
            return
        try:
            injector = PineInjector(tv_cdp)
            result = await injector.inject_and_compile(pine)
            if result["success"]:
                logger.info("[TV] Signals + positions reflected")
            else:
                logger.warning(f"[TV] Pine compile errors: {result['errors']}")
        finally:
            await tv_cdp.disconnect()
    except Exception as e:
        logger.warning(f"[TV] Chart visualization failed: {e}")


def _build_trading_runtime(config: AppConfig):
    """trading_cycle が必要とするランタイム (broker / adaptive_store / notifier / LLMs) を一括生成する。"""
    broker = create_broker(config.trading.trading_mode)
    adaptive_store = AdaptiveParamsStore(
        state_dir=config.state_dir,
        defaults={
            "sl_atr_mult": config.trading.sl_atr_mult_default,
            "tp_atr_mult": config.trading.tp_atr_mult_default,
        },
        limits={
            "sl_atr_mult_min": config.trading.sl_atr_mult_min,
            "sl_atr_mult_max": config.trading.sl_atr_mult_max,
            "tp_atr_mult_min": config.trading.tp_atr_mult_min,
            "tp_atr_mult_max": config.trading.tp_atr_mult_max,
        },
    )
    notifier = create_notifier(config.notifier.notifier)
    llm_price = create_llm_client(config, "price_analysis")
    llm_reflect = create_llm_client(config, "reflection")
    return broker, adaptive_store, notifier, llm_price, llm_reflect


async def trading_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    hold_store: HoldDecisionStore,
    price_provider: PriceProvider | None = None,
    session_store=None,
) -> None:
    """取引サイクル全体のオーケストレーター。"""
    run_start = datetime.now(ZoneInfo(config.schedule.timezone))
    logger.info(f"=== Trading cycle started: {run_start.strftime('%Y-%m-%d %H:%M %Z')} ===")

    broker, adaptive_store, notifier, llm_price, llm_reflect = _build_trading_runtime(config)
    logger.info(
        f"[TRADE] mode={config.trading.trading_mode} broker={type(broker).__name__} "
        f"notifier={type(notifier).__name__} "
        f"price={type(llm_price).__name__}({llm_price.model_name}) "
        f"reflect={type(llm_reflect).__name__}({llm_reflect.model_name})"
    )

    if not is_market_open(run_start):
        logger.info(f"Market {market_status_label(run_start)}. Skipping trading cycle.")
        return

    embed_fn = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )

    # Phase 1: SL/TP クローズ
    closed_this_run = await _phase_close_sl_tp(config, position_mgr, broker, notifier, price_provider)

    # Phase 1.5: 決済済オーダーの振り返り + adaptive params 更新 + RAG 蓄積
    await _finalize_closed_orders(
        closed_this_run, config, store, embed_fn, llm_reflect,
        adaptive_store, session_store, log_source="[REFLECT/CLOSE]",
    )

    # Phase 2: オープンポジションの振り返り生成
    await _generate_cycle_reflections(config, position_mgr, store, llm_reflect, price_provider=price_provider)

    # Phase 2.5: 前回 HOLD 判断のレビュー
    await _review_hold_decisions(config, hold_store, store, price_provider=price_provider, price_store=price_store)

    # Phase 3: 並列ペア分析
    signals, macro_ctxs = await _phase_analyze_pairs(
        config, position_mgr, store, price_store, analysis_store, llm_price, price_provider,
    )

    # Phase 4a: position_review (Layer 1-3) → 決済 → 振り返り
    reviewed_closed = await _phase_review_open_positions(
        config, position_mgr, signals, notifier, price_provider,
    )
    await _finalize_closed_orders(
        reviewed_closed, config, store, embed_fn, llm_reflect,
        adaptive_store, session_store, log_source="[REFLECT/REVIEW]",
    )

    # Phase 4b: 新規シグナル発注
    executed_orders = await _phase_execute_signals(
        signals, macro_ctxs, config, position_mgr, broker, notifier, store, price_store,
        hold_store, session_store, adaptive_store, embed_fn, price_provider,
    )

    # TradingView チャート反映
    await _phase_render_tradingview(config, analysis_store, position_mgr)

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
    price_provider: PriceProvider | None = None,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    from src.data.session_store import SessionStore
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="TradingCycle")
    session_store = SessionStore(config.prices_db_path)
    asyncio.run(trading_cycle(config, position_mgr, store, price_store, analysis_store, hold_store, price_provider=price_provider, session_store=session_store))


async def exit_check_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    price_provider: PriceProvider | None = None,
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
            current_prices[pos.pair] = _get_price(pos.pair, price_provider)
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
                source="exit_check",
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
        *[_summarize_pair(p, config, position_mgr, store, analysis_store, price_provider=price_provider) for p in relevant_cfgs],
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
                    source="exit_check",
                ))


def run_exit_check_cycle(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    price_provider: PriceProvider | None = None,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="ExitCheck")
    asyncio.run(exit_check_cycle(config, position_mgr, store, analysis_store, price_provider=price_provider))


async def forecast_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    forecast_store,
    price_provider: PriceProvider | None = None,
    price_store=None,
) -> None:
    """予測サイクル（設定間隔ごと実行）。

    LLM不使用。ノイズ対策 A+B+C+D を適用:
      A: ATR proxy による有意性フィルター（小動きはスキップ）
      B: 8h検証ウィンドウ（呼び出し間隔で制御）
      C: 高確信度シグナルのみ予測生成
      D: 事実文字列として RAG に蓄積（LLM解釈なし）
    """
    from src.analysis.forecaster import build_forecast_review, build_forecast_review_summary

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
                    current_price = _get_price(pair_cfg.symbol, price_provider)
                except Exception as e:
                    logger.warning(f"[FORECAST] {pair_cfg.symbol}: price fetch failed — {e}")
                    continue

                review_ts = datetime.now()

                pair_atr = _fetch_and_compute_atr(pair_cfg.symbol, config, price_store)

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
                    atr_value=pair_atr,
                )
                logger.info(f"[FORECAST] {pair_cfg.symbol}: {summary_text}")

                if has_significant:
                    embedding = await embed_fn(summary_text)
                    # レガシーfx_reflectionsへの書き込みは停止（方向別RAGに移行済み）

                # Directional RAG: individual forecast reviews
                for fc in recent_forecasts:
                    fc_text, _fc_lesson, fc_significant = build_forecast_review(
                        pair=pair_cfg.symbol,
                        forecast=fc,
                        current_price=current_price,
                        review_ts=review_ts,
                        significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
                        atr_value=pair_atr,
                    )
                    if fc_significant:
                        await record_forecast_review(
                            store, embed_fn, pair_cfg.symbol, fc, fc_text, current_price,
                        )

            # Phase 2: 新規予測生成（C: スコア閾値チェック、LLM不使用）
            signal = await _summarize_pair(pair_cfg, config, position_mgr, store, analysis_store, price_provider=price_provider)
            if signal is None:
                logger.info(f"[FORECAST] {pair_cfg.symbol}: skip — no_snapshot")
                continue

            # C: deadband を超えた高確信度シグナルのみ予測対象
            if abs(signal.combined_score) < config.analysis.forecast_min_combined_score:
                forecast_store.save_forecast_skip(pair_cfg.symbol, signal)
                continue

            macro_ctx = _build_macro_context(config, analysis_store)
            forecast_store.save_forecast(pair_cfg.symbol, signal, macro_context=macro_ctx)

            await record_forecast_entry(store, embed_fn, pair_cfg.symbol, signal, now)

        except Exception as e:
            logger.warning(f"[FORECAST] {pair_cfg.symbol}: error — {e}", exc_info=True)

    forecast_store.prune_old()
    logger.info("=== Forecast cycle complete ===")


def run_forecast_cycle(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    forecast_store,
    price_provider: PriceProvider | None = None,
    price_store=None,
) -> None:
    """scheduleライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="ForecastCycle")
    asyncio.run(forecast_cycle(config, position_mgr, store, analysis_store, forecast_store, price_provider=price_provider, price_store=price_store))


async def _summarize_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    price_provider: PriceProvider | None = None,
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
        current_price = _get_price(pair_cfg.symbol, price_provider)

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


# NOTE: view 系関数 (run_news_view / run_tech_view / run_analysis_summary /
# run_forecast_view / run_ask) は src/views.py に分離済み。
# _summarize_pair は forecast_cycle と analysis_summary の両方から使われるため
# trading_cycle.py に残置 (views.py から import される)。
