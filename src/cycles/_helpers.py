"""サイクル (exit_check / views / api) で共有する純ヘルパー群。

これらの関数は副作用が小さく、テストから直接呼ぶことも想定している
(``tests/test_cycle_helpers.py``)。サイクル本体に閉じた処理は
``exit_check.py`` / ``reflection.py`` に置く。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_fetcher import fetch_current_price
from src.data.price_provider import PriceProvider
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


# ──────────────────────────────────────────────────────────────────────
# 共有シグナル要約 (exit_check / views / api から呼ばれる)
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
            min_rr_ratio=config.trading.min_rr_ratio,
        )
    except Exception as e:
        logger.error(f"Failed to summarize {pair_cfg.display_name}: {e}", exc_info=True)
        return None


__all__ = [
    "_get_price",
    "_summarize_pair",
]
