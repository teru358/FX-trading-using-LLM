"""予測検証サイクル (LLM 不使用)。

設定間隔ごとに走る軽量サイクル。ノイズ対策 A+B+C+D を適用:
  A: ATR proxy による有意性フィルター (小動きはスキップ)
  B: 8h 検証ウィンドウ (呼び出し間隔で制御)
  C: 高確信度シグナルのみ予測生成
  D: 事実文字列として RAG に蓄積 (LLM 解釈なし)
"""
from __future__ import annotations

import asyncio
import logging
from src.config import AppConfig
from src.cycles._helpers import (
    _build_macro_context,
    _fetch_and_compute_atr,
    _get_price,
    _summarize_pair,
)
from src.data.analysis_store import AnalysisStore
from src.data.price_provider import PriceProvider
from src.persistence.state_store import StateStore
from src.rag.directional_writer import record_forecast_entry, record_forecast_review
from src.rag.embedder import make_embed_fn
from src.rag.vector_store import VectorStore
from src.trading.market_hours import is_market_open
from src.trading.position_manager import PositionManager
from src.utils.clock import db_now, local_now

logger = logging.getLogger(__name__)


async def forecast_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    forecast_store,
    price_provider: PriceProvider | None = None,
    price_store=None,
) -> None:
    """予測サイクル (設定間隔ごと実行)。"""
    from src.analysis.forecaster import build_forecast_review, build_forecast_review_summary

    now = local_now(config)

    if not is_market_open(now):
        return

    logger.info(f"=== Forecast cycle: {now.strftime('%H:%M %Z')} ===")

    embed_fn = make_embed_fn(config)

    for pair_cfg in config.tradeable_instruments:
        try:
            # Phase 1: 直近 24h の全予測を毎サイクル更新検証 (D)
            recent_forecasts = forecast_store.get_recent_forecasts(pair_cfg.symbol, hours=24)
            if recent_forecasts:
                try:
                    current_price = _get_price(pair_cfg.symbol, price_provider)
                except Exception as e:
                    logger.warning(f"[FORECAST] {pair_cfg.symbol}: price fetch failed — {e}")
                    continue

                review_ts = db_now()
                pair_atr = _fetch_and_compute_atr(
                    pair_cfg.symbol, config, price_store,
                    atr_timeframe=config.trading.atr_timeframe,
                )

                # 各予測の delta を更新
                for fc in recent_forecasts:
                    delta = current_price - fc.current_price
                    forecast_store.update_review(fc.id, delta)

                # 24h 集計サマリーをログ出力のみ (RAG 蓄積は下の individual forecast reviews で行う)
                summary_text, lesson, has_significant = build_forecast_review_summary(
                    pair=pair_cfg.symbol,
                    forecasts=recent_forecasts,
                    current_price=current_price,
                    review_ts=review_ts,
                    significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
                    atr_value=pair_atr,
                )
                logger.info(f"[FORECAST] {pair_cfg.symbol}: {summary_text}")

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

            # Phase 2: 新規予測生成 (C: スコア閾値チェック、LLM 不使用)
            signal = await _summarize_pair(
                pair_cfg, config, position_mgr, store, analysis_store, price_provider=price_provider,
            )
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
    """schedule ライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="ForecastCycle")
    asyncio.run(forecast_cycle(
        config, position_mgr, store, analysis_store, forecast_store,
        price_provider=price_provider, price_store=price_store,
    ))
