"""出口専用軽量サイクル (LLM 不使用)。

毎時 :00 に走り、SL/TP に到達した既存ポジションを決済し、
position_review (Layer 1-3) のみキャッシュ済みスナップショットから判定する。
新規発注・振り返り生成・LLM 呼び出しは一切行わない。
"""
from __future__ import annotations

import asyncio
import logging

from src.config import AppConfig
from src.cycles._helpers import _get_price, _summarize_pair
from src.data.analysis_store import AnalysisStore
from src.data.price_provider import PriceProvider
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.rag.vector_store import VectorStore
from src.trading.live_broker import create_broker
from src.trading.market_hours import is_market_open
from src.trading.position_manager import PositionManager
from src.trading.position_reviewer import review_open_positions
from src.utils.clock import local_now

logger = logging.getLogger(__name__)

_LAYER_LABEL = {"reversal": "L1", "timeout": "L2", "profit_lock": "L3"}


async def exit_check_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
    price_provider: PriceProvider | None = None,
) -> None:
    """出口専用軽量サイクル (毎時 :00 実行)。

    LLM 不使用。キャッシュ済みスナップショットを集約して
    SL/TP 確認とポジション再評価 (Layer 1-3) のみ実行する。
    新規発注・振り返り生成はスキップ。
    """
    run_start = local_now(config)

    if not is_market_open(run_start):
        return

    account = position_mgr.get_account_state()
    if not account.open_positions:
        return

    logger.info(
        f"=== Exit check: {run_start.strftime('%H:%M %Z')} "
        f"({len(account.open_positions)} open positions) ==="
    )

    broker = create_broker(
        config.trading.trading_mode,
        mt5_bridge_url=config.mt5_bridge.bridge_url,
        mt5_lot_size_units=config.mt5_bridge.lot_size_units,
        mt5_magic_number=config.mt5_bridge.magic_number,
        mt5_order_timeout_seconds=config.mt5_bridge.order_request_timeout_seconds,
        shadow_log_path=config.mt5_bridge.shadow_log_path,
        shadow_observer_state_dir=config.mt5_bridge.shadow_observer_state_dir,
        initial_balance=config.trading.initial_balance,
    )
    notifier = create_notifier(config.notifier.enabled)

    # Phase 1: SL/TP 確認
    current_prices: dict[str, float] = {}
    for pos in account.open_positions:
        try:
            current_prices[pos.pair] = _get_price(pos.pair, price_provider)
        except Exception as e:
            logger.warning(f"[EXIT] Could not fetch price for {pos.pair}: {e}")

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
                source="exit_check",
            ))

    # Phase 4a: ポジション再評価 (キャッシュ集約のみ、LLM 不使用)
    if not config.trading.position_review_enabled:
        return

    account_for_review = position_mgr.get_account_state()
    if not account_for_review.open_positions:
        return

    open_pairs = {pos.pair for pos in account_for_review.open_positions}
    relevant_cfgs = [p for p in config.tradeable_instruments if p.symbol in open_pairs]

    sig_results = await asyncio.gather(
        *[
            _summarize_pair(p, config, position_mgr, store, analysis_store, price_provider=price_provider)
            for p in relevant_cfgs
        ],
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
        # broker 経由で close (mt5_bridge live モードでは MT5 close → 内部 close、
        # paper モードでは内部 close のみ)。これにより MT5 側に残ポジが残って次
        # サイクル reconciliation で hard halt するのを防ぐ。
        closed_order = broker.close_position(
            decision.order_id, price, decision.close_reason, position_mgr,
        )
        if closed_order:
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
    """schedule ライブラリから呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(
        state_store, config.trading.initial_balance, context="ExitCheck",
    )
    asyncio.run(exit_check_cycle(
        config, position_mgr, store, analysis_store, price_provider=price_provider,
    ))
