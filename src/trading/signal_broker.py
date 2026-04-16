"""Signal ブローカーアダプター。

注文を発注せず、シグナル推奨通知と manual ポジションの SL/TP アラートのみを行う。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.notifications.notifier import SignalRecommendationEvent, SLTPAlertEvent
from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.portfolio_guard import check_drawdown_kill_switch, check_portfolio_limits
from src.trading.position_manager import Order, PositionManager

if TYPE_CHECKING:
    from src.notifications.notifier import NotifierAdapter

logger = logging.getLogger(__name__)

_LOG = "[SIGNAL-REC]"


class SignalBrokerAdapter(BrokerAdapter):
    """注文を発注しないシグナル通知専用ブローカー。

    - execute_signal: hold 以外のシグナルを推奨通知として発火し、常に None を返す。
    - check_and_close_positions: manual ポジションの SL/TP 到達を検出して通知し、空リストを返す。
    """

    def __init__(
        self,
        manual_position_mgr: PositionManager,
        notifier: "NotifierAdapter",
        max_total_positions: int = 4,
        max_positions_per_group: int = 2,
        max_same_direction_per_group: int = 2,
        drawdown_kill_switch_enabled: bool = False,
        drawdown_kill_switch_max_pct: float = 0.10,
        drawdown_kill_switch_lookback_days: int = 0,
    ) -> None:
        self._manual_mgr = manual_position_mgr
        self._notifier = notifier
        self._max_total = max_total_positions
        self._max_per_group = max_positions_per_group
        self._max_same_dir = max_same_direction_per_group
        self._dd_enabled = drawdown_kill_switch_enabled
        self._dd_max_pct = drawdown_kill_switch_max_pct
        self._dd_lookback = drawdown_kill_switch_lookback_days

    def _fire(self, coro) -> None:
        """非同期コルーチンをイベントループに投げる。ループがなければ無視する。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            pass

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        """シグナルを推奨通知として発火する。注文は発注しない。"""
        if signal.action == "hold":
            logger.debug("%s HOLD シグナルのため通知スキップ: %s", _LOG, signal.pair)
            return None

        # manual ポジションを対象にポートフォリオガード判定
        manual_account = self._manual_mgr.get_account_state()
        manual_positions = manual_account.open_positions
        portfolio_warning = check_portfolio_limits(
            pair=signal.pair,
            direction=signal.action,
            position_size=signal.position_size,
            open_positions=manual_positions,
            max_positions_per_group=self._max_per_group,
            max_total_positions=self._max_total,
            max_same_direction_per_group=self._max_same_dir,
        ) or ""

        # Drawdown kill switch — 警告メッセージに追加するのみ (signal モードは発注しないため)
        dd_warning = check_drawdown_kill_switch(
            initial_balance=manual_account.initial_balance,
            closed_trades=manual_account.closed_trades,
            enabled=self._dd_enabled,
            max_drawdown_pct=self._dd_max_pct,
            lookback_days=self._dd_lookback,
        )
        if dd_warning:
            portfolio_warning = (
                f"{portfolio_warning} | {dd_warning}" if portfolio_warning else dd_warning
            )

        # 同一ペアの既存 manual ポジション数
        existing_positions = sum(1 for p in manual_positions if p.pair == signal.pair)

        # 最大損失（残高 × 1% をデフォルトとして使用）
        balance = self._manual_mgr.get_account_state().balance
        max_loss = balance * 0.01

        event = SignalRecommendationEvent(
            pair=signal.pair,
            direction=signal.action,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=signal.position_size,
            combined_score=signal.combined_score,
            confidence=signal.confidence,
            signal_reason=signal.signal_reason,
            detail_reason=signal.detail_reason,
            max_loss=max_loss,
            portfolio_warning=portfolio_warning,
            existing_positions=existing_positions,
            source="signal",
        )
        logger.info("%s シグナル推奨通知: %s %s score=%.3f", _LOG, signal.pair, signal.action, signal.combined_score)
        self._fire(self._notifier.notify_signal_recommendation(event))
        return None

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        """manual ポジションの SL/TP 到達を検出して通知する。決済は行わない。"""
        manual_positions = self._manual_mgr.get_account_state().open_positions

        for order in manual_positions:
            price = current_prices.get(order.pair)
            if price is None:
                continue

            trigger = _detect_sltp(order, price)
            if trigger is None:
                continue

            unrealized_pnl = _calc_unrealized_pnl(order, price)
            event = SLTPAlertEvent(
                pair=order.pair,
                direction=order.direction,
                order_id=order.order_id,
                entry_price=order.entry_price,
                current_price=price,
                trigger=trigger,
                unrealized_pnl=unrealized_pnl,
            )
            logger.info("%s SL/TP アラート: %s %s trigger=%s", _LOG, order.pair, order.direction, trigger)
            self._fire(self._notifier.notify_sltp_alert(event))

        return []


def _detect_sltp(order: Order, current_price: float) -> str | None:
    """SL/TP 到達を検出し、"stop_loss" | "take_profit" | None を返す。"""
    if order.direction == "buy":
        if current_price <= order.stop_loss:
            return "stop_loss"
        if current_price >= order.take_profit:
            return "take_profit"
    else:  # sell
        if current_price >= order.stop_loss:
            return "stop_loss"
        if current_price <= order.take_profit:
            return "take_profit"
    return None


def _calc_unrealized_pnl(order: Order, current_price: float) -> float:
    """未実現損益を計算する（簡易: pips × position_size）。"""
    if order.direction == "buy":
        return (current_price - order.entry_price) * order.position_size
    else:
        return (order.entry_price - current_price) * order.position_size
