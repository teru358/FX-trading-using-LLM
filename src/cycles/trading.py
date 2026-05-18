"""フル取引サイクル (LLM 使用、新規発注あり)。

Phase 1: SL/TP に到達した既存ポジションを決済 (broker)
Phase 1.5: 決済済みオーダーの振り返り → adaptive params 更新 → RAG 蓄積
Phase 2.5: 前回 HOLD 判断のレビュー (LLM 不使用)
Phase 3: 並列ペア分析 (LLM 使用) → シグナル + macro_ctx
Phase 4a: position_review (Layer1-3) → 早期決済 → 振り返り
Phase 4b: 新規シグナルの RAG 補正 + 発注 / HOLD 保存
Phase 5: ランサマリー
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.analysis.price_analyzer import analyze_price_action, load_user_notes
from src.analysis.reflector import generate_close_reflection
from src.config import AppConfig
from src.cycles._helpers import (
    _build_macro_context,
    _build_rag_context,
    _compute_atr_from_price_data,
    _get_ohlcv,
    _get_price,
)
from src.data.analysis_store import AnalysisStore, HoldDecisionStore
from src.data.indicators import compute_indicators
from src.data.price_provider import PriceProvider
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.notifications.notifier import (
    OrderClosedEvent,
    OrderOpenedEvent,
    SignalSkippedEvent,
    create_notifier,
)
from src.persistence.adaptive_params_store import AdaptiveParamsStore
from src.persistence.state_store import StateStore
from src.rag.directional_writer import (
    record_hold_review,
    record_trade_complete,
    record_trade_entry,
)
from src.rag.embedder import make_embed_fn
from src.rag.vector_store import VectorStore
from src.reporting.reporter import print_run_summary
from src.signals.rag_adjustment import RagAdjustmentConfig, compute_rag_adjustment
from src.trading.atr_calculator import calculate_sl_tp
from src.trading.entry_context_builder import build_entry_context
from src.trading.live_broker import create_broker
from src.trading.market_hours import is_market_open
from src.trading.position_manager import PositionManager
from src.trading.position_reviewer import review_open_positions
from src.utils.clock import db_now, local_now

if TYPE_CHECKING:
    from src.trading.bridge_health_gate import BridgeHealthGate

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# ペア処理 / SL/TP 適用
# ──────────────────────────────────────────────────────────────────────

async def _process_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
    price_provider: PriceProvider | None = None,
    forecast_store=None,
):
    """1ペアの分析→シグナル生成。

    テクニカル分析は収集ジョブで蓄積済みのスナップショットを集約して使用する。
    スナップショットが存在しない場合 (初回起動直後など) は Ollama 即時分析にフォールバック。
    戻り値: (TradeSignal, macro_ctx_str)
    """
    from src.analysis.news_aggregator import aggregate_news_sentiment
    from src.signals.signal_combiner import combine_signals
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
            tech_score = compute_technical_score(
                summary,
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

        # Forecast accuracy auto-feedback の provider を構築
        # forecast_store 未提供なら provider=None で従来動作
        accuracy_provider = None
        if forecast_store is not None and config.trading.forecast_accuracy_feedback.enabled:
            from functools import partial
            from src.signals.accuracy_tracker import compute_recent_accuracy
            accuracy_provider = partial(
                compute_recent_accuracy,
                forecast_store,
                hours=config.trading.forecast_accuracy_feedback.lookback_hours,
            )

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
            min_rr_ratio=config.trading.min_rr_ratio,
            accuracy_provider=accuracy_provider,
            accuracy_config=config.trading.forecast_accuracy_feedback,
        )
        return signal, macro_ctx
    except Exception as e:
        logger.error(f"Failed to process {pair_cfg.display_name}: {e}", exc_info=True)
        return e


def _apply_atr_sltp_to_signal(
    sig,
    config: AppConfig,
    position_mgr: PositionManager,
    price_store: PriceStore,
    adaptive_store: AdaptiveParamsStore,
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
        atr_val = _compute_atr_from_price_data(
            price_data, resample_tf=config.trading.atr_timeframe,
        )
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

        # ATR 上書き後の R:R 再チェック (combine_signals の LLM R:R チェックを通過した後に
        # ATR で SL/TP が変わるため、最終 R:R が min_rr_ratio を下回る可能性がある)
        if config.trading.min_rr_ratio > 0 and sig.action in ("buy", "sell"):
            sl_dist = abs(sig.entry_price - sltp_result.computed_sl)
            tp_dist = abs(sltp_result.computed_tp - sig.entry_price)
            actual_rr = tp_dist / sl_dist if sl_dist > 0 else 0
            if actual_rr < config.trading.min_rr_ratio:
                logger.info(
                    f"[SIGNAL] {sig.pair}: ATR R:R too low "
                    f"({actual_rr:.2f} < {config.trading.min_rr_ratio:.2f}) → hold"
                )
                sig.action = "hold"
                sig.signal_reason = (
                    f"ATR R:R too low ({actual_rr:.2f} < {config.trading.min_rr_ratio:.2f})"
                )
                return sltp_result

        # ボラレジームによるリスク倍率適用
        effective_risk_pct = config.trading.risk_per_trade
        if config.trading.vol_regime_enabled:
            from src.signals.vol_regime import compute_vol_regime
            # ATR と同じ足種でリサンプルして vol_regime を計算
            vr_df = price_data.df
            _vr_tf = config.trading.atr_timeframe
            if _vr_tf and _vr_tf not in ("", "1h"):
                vr_df = vr_df.resample(_vr_tf).agg({
                    "Open": "first", "High": "max", "Low": "min",
                    "Close": "last", "Volume": "sum",
                }).dropna()
            vr = compute_vol_regime(
                vr_df,
                ewma_span=config.trading.vol_regime_ewma_span,
                high_threshold=config.trading.vol_regime_high_threshold,
                low_threshold=config.trading.vol_regime_low_threshold,
                high_risk_scale=config.trading.vol_regime_high_risk_scale,
                low_risk_scale=config.trading.vol_regime_low_risk_scale,
            )
            if vr:
                effective_risk_pct *= vr.risk_scale
                logger.info(
                    f"[VOL-REGIME] {sig.pair}: {vr.regime} "
                    f"(ATR={vr.atr:.5f} EWMA={vr.ewma_atr:.5f} ratio={vr.ratio:.3f}) "
                    f"risk×{vr.risk_scale:.2f}"
                )

        from src.signals.signal_combiner import _calculate_position_size
        pair_cfg_for_size = next(
            (p for p in config.tradeable_instruments if p.symbol == sig.pair), None
        )
        if pair_cfg_for_size:
            sig.position_size = _calculate_position_size(
                balance=position_mgr.get_account_state().balance,
                risk_pct=effective_risk_pct,
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


# ──────────────────────────────────────────────────────────────────────
# 決済済オーダーの後処理 / HOLD レビュー
# ──────────────────────────────────────────────────────────────────────

async def _finalize_closed_orders(
    closed_orders: list,
    config: AppConfig,
    store: VectorStore,
    embed_fn,
    llm_reflect: LLMClient,
    adaptive_store: AdaptiveParamsStore,
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
            macro_ctx_at_entry = ""
            if session_store:
                sess = session_store.get_session(closed_order.order_id)
                if sess:
                    entry_analysis = sess.analysis_summary or ""
                    macro_ctx_at_entry = sess.macro_context or ""
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
                macro_context_at_entry=macro_ctx_at_entry,
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
                    closed_at=closed_order.closed_at or db_now(),
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
    """前回 HOLD した判断を検証して RAG に蓄積する (LLM 不使用)。"""
    from src.analysis.forecaster import build_hold_review
    from src.cycles._helpers import _fetch_and_compute_atr

    unreviewed = hold_store.get_unreviewed()
    if not unreviewed:
        return

    logger.info(f"[HOLD REVIEW] Reviewing {len(unreviewed)} hold decision(s)")
    embed_fn = make_embed_fn(config)

    for hold in unreviewed:
        try:
            current_price = _get_price(hold.pair, price_provider)
            hold_atr = _fetch_and_compute_atr(
                hold.pair, config, price_store,
                atr_timeframe=config.trading.atr_timeframe,
            )

            review_text, lesson, worth_storing = build_hold_review(
                pair=hold.pair,
                hold=hold,
                current_price=current_price,
                review_ts=db_now(),
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


# ──────────────────────────────────────────────────────────────────────
# 取引サイクルの各 Phase
# ──────────────────────────────────────────────────────────────────────

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
    forecast_store=None,
) -> tuple[list, dict[str, str]]:
    """Phase 3: 全ペアを並列分析してシグナル + macro_ctx を生成する。"""
    semaphore = asyncio.Semaphore(config.llm.provider_config.max_concurrent)

    async def bounded(pair_cfg):
        async with semaphore:
            return await _process_pair(
                pair_cfg, config, position_mgr, store, price_store, analysis_store, llm_price,
                price_provider=price_provider,
                forecast_store=forecast_store,
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
    broker,
    *,
    timeout_only: bool = False,
) -> list:
    """Phase 4a: position_review (Layer 1-3) を実行し決済済オーダーを返す。

    timeout_only=True: halt 中などで Layer 2 のみ評価する。
                       position_review_enabled の gate を bypass する
                       (timeout は安全網なので常に動く)。
    """
    # 通常 cycle: position_review_enabled=False なら skip
    # halt timeout (timeout_only=True): フラグに関わらず常に評価
    if not config.trading.position_review_enabled and not timeout_only:
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
        timeout_only=timeout_only,
        reversal_confidence_min=config.trading.reversal_confidence_min,
        reversal_score_threshold=config.trading.reversal_score_threshold,
        reversal_min_holding_minutes=config.trading.reversal_min_holding_minutes,
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
        # broker は引数で受け取った値を使う (latent bug 解消)
        closed_order = broker.close_position(
            decision.order_id, price, decision.close_reason, position_mgr,
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
    """方向別 RAG の過去成績をもとにシグナルスコアを補正し、必要なら action も再判定する。"""
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
    adaptive_store: AdaptiveParamsStore,
    embed_fn_adj,
    price_provider: PriceProvider | None,
):
    """1シグナルの発注処理 (ATR SL/TP → broker → session → RAG 記録) を実行する。"""
    sltp_result = _apply_atr_sltp_to_signal(
        sig, config, position_mgr, price_store, adaptive_store,
        price_provider=price_provider,
    )

    # ATR SL/TP 算出に失敗した場合はエントリーしない (SL/TP=0 で約定すると即決済になる)
    if sltp_result is None and sig.action in ("buy", "sell"):
        logger.warning(
            f"[SIGNAL] {sig.pair}: ATR SL/TP unavailable — skipping entry"
        )
        sig.action = "hold"
        sig.signal_reason = "ATR SL/TP calculation failed"

    result = broker.execute_signal(sig, position_mgr, macro_context=macro_ctx)
    if not result.is_executed:
        if config.notifier.notify_on_signal_skipped:
            await notifier.notify_signal_skipped(SignalSkippedEvent(
                pair=sig.pair,
                action=sig.action,
                confidence=sig.confidence,
                signal_reason=sig.signal_reason,
                detail_reason=sig.detail_reason,
                source="trading",
                outcome=result.outcome,
                skip_reason=result.reason,
            ))
        return None
    order = result.order

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
            is_scale_in=order.is_scale_in,
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
    adaptive_store: AdaptiveParamsStore,
    embed_fn_adj,
    price_provider: PriceProvider | None,
) -> list:
    """Phase 4b: シグナルに RAG 補正を適用し、新規発注 or HOLD 保存を実行する。"""
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


# ──────────────────────────────────────────────────────────────────────
# ランタイム生成 / オーケストレーター / 同期ラッパー
# ──────────────────────────────────────────────────────────────────────

def _build_trading_runtime(config: AppConfig):
    """trading_cycle が必要とするランタイム (broker / adaptive_store / notifier / LLMs) を一括生成する。

    Returns:
        (broker, adaptive_store, notifier, llm_price, llm_reflect)
    """
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
    llm_price = create_llm_client(config, "price_analysis")
    llm_reflect = create_llm_client(config, "reflection")

    _dd_kwargs = {
        "drawdown_kill_switch_enabled": config.trading.drawdown_kill_switch_enabled,
        "drawdown_kill_switch_max_pct": config.trading.drawdown_kill_switch_max_pct,
    }

    # Phase 3b: notifier を broker 構築前に生成 (mt5_bridge / shadow が利用)
    notifier = create_notifier(config.notifier.enabled)
    mt5_cfg = config.providers.mt5
    broker = create_broker(
        config.mode,
        config.live_broker,
        max_positions_per_pair=config.trading.max_positions_per_pair,
        scale_in_enabled=config.trading.scale_in_enabled,
        scale_in_conf_margin=config.trading.scale_in_conf_margin,
        scale_in_score_margin=config.trading.scale_in_score_margin,
        notifier=notifier,
        state_dir=config.state_dir,
        mt5_bridge_url=(mt5_cfg.bridge_url if mt5_cfg else ""),
        mt5_lot_size_units=(mt5_cfg.lot_size_units if mt5_cfg else 100_000),
        mt5_magic_number=(mt5_cfg.magic_number if mt5_cfg else 12345),
        mt5_order_timeout_seconds=(mt5_cfg.order_request_timeout_seconds if mt5_cfg else 10.0),
        mt5_consecutive_reject_threshold=(mt5_cfg.consecutive_reject_threshold if mt5_cfg else 3),
        live_test_log_path=(mt5_cfg.shadow_log_path if mt5_cfg else "data/state/shadow_trades.jsonl"),
        live_test_observer_state_dir=(mt5_cfg.shadow_observer_state_dir if mt5_cfg else "data/shadow_state"),
        **_dd_kwargs,
    )

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
    forecast_store=None,
) -> None:
    """取引サイクル全体のオーケストレーター。"""
    run_start = local_now(config)
    logger.info(f"=== Trading cycle started: {run_start.strftime('%Y-%m-%d %H:%M %Z')} ===")

    broker, adaptive_store, notifier, llm_price, llm_reflect = _build_trading_runtime(config)

    logger.info(
        f"[TRADE] mode={config.mode} broker={type(broker).__name__} "
        f"notifier={type(notifier).__name__} "
        f"price={type(llm_price).__name__}({llm_price.model_name}) "
        f"reflect={type(llm_reflect).__name__}({llm_reflect.model_name})"
    )

    if not is_market_open(run_start):
        # 休場中は無音スキップ (MarketStateTracker が遷移/ハートビートのみログ化)
        return

    # Task 8: balance.json staleness check (live mode のみ警告)
    _BALANCE_STALE_THRESHOLD_MIN = 30
    if config.mode in ("live", "live_test"):
        from datetime import datetime, timezone

        from src.persistence import balance_snapshot
        snap = balance_snapshot.read(config.state_dir)
        if snap.source == "mt5" and balance_snapshot.is_stale(
            snap, threshold_minutes=_BALANCE_STALE_THRESHOLD_MIN,
        ):
            age_min = (
                datetime.now(tz=timezone.utc)
                - datetime.fromisoformat(snap.fetched_at)
            ).total_seconds() / 60.0
            logger.warning(
                f"[TRADE] balance.json stale: age={age_min:.0f}min "
                f"(threshold={_BALANCE_STALE_THRESHOLD_MIN}min) "
                f"fetched_at={snap.fetched_at} — using last MT5 value for lot calc"
            )

    embed_fn = make_embed_fn(config)

    # Phase 1: SL/TP クローズ
    closed_this_run = await _phase_close_sl_tp(config, position_mgr, broker, notifier, price_provider)

    # Phase 1.5: 決済済オーダーの振り返り + adaptive params 更新 + RAG 蓄積
    if closed_this_run:
        for co in closed_this_run:
            logger.info(f"[CLOSE] Closed {co.pair} ({co.close_reason}) pnl={co.realized_pnl or 0:.2f}")
    await _finalize_closed_orders(
        closed_this_run, config, store, embed_fn, llm_reflect,
        adaptive_store, session_store, log_source="[REFLECT/CLOSE]",
    )

    # Phase 2.5: 前回 HOLD 判断のレビュー
    await _review_hold_decisions(config, hold_store, store, price_provider=price_provider, price_store=price_store)

    # halt 中は新規エントリー分析・発注を skip (既存ポジ管理 Phase 1〜2.5 は継続済)
    # 二重チェック: ここと execute_signal 入口の両方で is_halted を確認する。
    from src.persistence import halt_state
    if halt_state.is_halted(config.state_dir):
        logger.info(
            "[CYCLE] soft-halted — running Layer 2 (timeout) only, "
            "skipping new entry analysis"
        )
        timeout_closed = await _phase_review_open_positions(
            config, position_mgr, signals=[],
            notifier=notifier, price_provider=price_provider,
            broker=broker, timeout_only=True,
        )
        await _finalize_closed_orders(
            timeout_closed, config, store, embed_fn, llm_reflect,
            adaptive_store, session_store, log_source="[REFLECT/TIMEOUT_HALT]",
        )
        return

    # Phase 3: 並列ペア分析
    signals, macro_ctxs = await _phase_analyze_pairs(
        config, position_mgr, store, price_store, analysis_store, llm_price, price_provider,
        forecast_store=forecast_store,
    )

    # Phase 4a: position_review (Layer 1-3) → 決済 → 振り返り
    reviewed_closed = await _phase_review_open_positions(
        config, position_mgr, signals, notifier, price_provider, broker,
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
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。

    gate が渡されたら冒頭で probe する。probe 失敗時は gate 内で halt_state.trigger_auto
    が走るため、その後の trading_cycle が Phase 2.5 → Phase 3 の入口で is_halted を読んで
    新規エントリー分析だけを skip する (Phase 1 SL/TP・Phase 1.5 reflection・Phase 2.5
    HOLD review は halt 中でも継続する設計、spec section 7「既存ポジ管理は継続」)。
    したがってここでは probe するのみで、結果が ok=False でも trading_cycle に進む。
    """
    if gate is not None:
        gate.probe(caller="trading", sync_balance=True)
    from src.data.analysis_store import ForecastStore
    from src.data.session_store import SessionStore
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="TradingCycle")
    session_store = SessionStore(config.prices_db_path)
    forecast_store = ForecastStore(config.prices_db_path)
    asyncio.run(trading_cycle(
        config, position_mgr, store, price_store, analysis_store, hold_store,
        price_provider=price_provider, session_store=session_store,
        forecast_store=forecast_store,
    ))