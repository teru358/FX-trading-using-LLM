from __future__ import annotations

import logging

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order, PositionManager

logger = logging.getLogger(__name__)


def execute_signal(signal: TradeSignal, position_mgr: PositionManager) -> Order | None:
    if signal.action == "hold":
        return None

    # One position per pair
    existing = position_mgr.get_open_position(signal.pair)
    if existing is not None:
        logger.info(
            f"[SKIP] {signal.pair} already has open position {existing.order_id}. "
            f"Skipping new {signal.action} signal."
        )
        return None

    direction = "buy" if signal.action == "buy" else "sell"
    order = Order.new(
        pair=signal.pair,
        direction=direction,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        position_size=signal.position_size,
        signal_reason=signal.signal_reason,
    )
    position_mgr.open_position(order)
    logger.info(
        f"[ORDER] {signal.pair} {direction.upper()} executed | "
        f"reason: {signal.signal_reason}"
    )
    return order


def check_and_close_positions(
    open_positions: list[Order],
    current_prices: dict[str, float],
    position_mgr: PositionManager,
) -> list[Order]:
    closed = []
    for pos in list(open_positions):
        price = current_prices.get(pos.pair)
        if price is None:
            logger.warning(f"No current price for {pos.pair}, cannot check SL/TP.")
            continue

        close_reason: str | None = None

        if pos.direction == "buy":
            if price <= pos.stop_loss:
                close_reason = "stop_loss"
            elif price >= pos.take_profit:
                close_reason = "take_profit"
        else:  # sell
            if price >= pos.stop_loss:
                close_reason = "stop_loss"
            elif price <= pos.take_profit:
                close_reason = "take_profit"

        if close_reason:
            logger.info(
                f"[CLOSE] {pos.pair} {pos.direction.upper()} hit {close_reason.upper()} | "
                f"entry={pos.entry_price:.5f} current={price:.5f}"
            )
            closed_order = position_mgr.close_position(pos.order_id, price, close_reason)
            if closed_order:
                closed.append(closed_order)

    return closed
