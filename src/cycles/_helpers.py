"""3つのトレーディングサイクル (trading / exit_check / forecast) で共有する純ヘルパー群。

これらの関数は副作用が小さく、テストから直接呼ぶことも想定している
(``tests/test_trading_cycle_helpers.py``)。サイクル本体に閉じた処理は
それぞれの ``trading.py`` / ``exit_check.py`` / ``forecast.py`` に置く。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_fetcher import fetch_current_price, fetch_ohlcv
from src.data.price_provider import PriceProvider
from src.data.price_store import PriceStore
from src.rag.prompt_formatter import (
    format_macro_context_for_prompt,
    format_news_for_prompt,
    format_reflections_for_prompt,
)
from src.rag.vector_store import VectorStore
from src.signals.signal_combiner import combine_signals

if TYPE_CHECKING:
    from src.trading.position_manager import PositionManager

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 価格 / OHLCV / ATR
# ──────────────────────────────────────────────────────────────────────

def _get_price(symbol: str, price_provider: PriceProvider | None) -> float:
    """price_provider 経由で現在価格を取得する。None の場合は直接呼び出しにフォールバック。"""
    if price_provider:
        return price_provider.get_current_price(symbol).price
    return fetch_current_price(symbol).price


def _get_ohlcv(
    symbol: str,
    period: str,
    interval: str,
    price_store,
    price_provider: PriceProvider | None,
):
    """price_provider 経由で OHLCV を取得する。None の場合は直接呼び出しにフォールバック。"""
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


# ──────────────────────────────────────────────────────────────────────
# RAG / マクロコンテキスト
# ──────────────────────────────────────────────────────────────────────

async def _build_rag_context(
    pair_cfg, config: AppConfig, store: VectorStore
) -> tuple[str, str]:
    """RAG からニュースと振り返りコンテキストを取得する。"""
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

    # 経済指標分析レポートを reflection_context に付加
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
                    f"[ECON] Injected {len(econ_reports)} econ analyses into "
                    f"{pair_cfg.display_name} context"
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


# ──────────────────────────────────────────────────────────────────────
# 共有シグナル要約 (exit_check / forecast / views / api から呼ばれる)
# ──────────────────────────────────────────────────────────────────────

async def _summarize_pair(
    pair_cfg,
    config: AppConfig,
    position_mgr: "PositionManager",
    store: VectorStore,
    analysis_store: AnalysisStore,
    price_provider: PriceProvider | None = None,
):
    """保存済み分析スナップショットとニュースからシグナルを算出する (新規取得なし)。"""
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


# datetime を import 元として保持しておくことで、shim 経由で `from src.trading_cycle
# import datetime` のような暗黙参照があっても壊れないようにする (歴史的経緯)。
__all__ = [
    "_get_price",
    "_get_ohlcv",
    "_compute_atr_from_price_data",
    "_fetch_and_compute_atr",
    "_build_rag_context",
    "_build_macro_context",
    "_summarize_pair",
    "datetime",
]