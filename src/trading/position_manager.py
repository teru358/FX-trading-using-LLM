from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from src.persistence.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class Order:
    order_id: str
    pair: str
    direction: str  # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    status: str = "open"  # "open" | "closed" | "cancelled"
    opened_at: datetime = field(default_factory=datetime.now)
    closed_at: Optional[datetime] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None  # "take_profit" | "stop_loss" | "manual"
    realized_pnl: Optional[float] = None
    signal_reason: str = ""  # エントリー理由（決済時の振り返りに使用）
    macro_context_at_entry: str = ""  # エントリー時の監視銘柄スナップショット（振り返りに使用）

    @staticmethod
    def new(
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        signal_reason: str = "",
        macro_context_at_entry: str = "",
    ) -> "Order":
        return Order(
            order_id=str(uuid.uuid4()),
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            signal_reason=signal_reason,
            macro_context_at_entry=macro_context_at_entry,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        d["closed_at"] = self.closed_at.isoformat() if self.closed_at else None
        return d

    @staticmethod
    def from_dict(d: dict) -> "Order":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        if d.get("closed_at"):
            d["closed_at"] = datetime.fromisoformat(d["closed_at"])
        d.setdefault("macro_context_at_entry", "")
        return Order(**d)


@dataclass
class AccountState:
    balance: float
    initial_balance: float
    open_positions: list[Order]
    closed_trades: list[Order]

    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.closed_trades if (t.realized_pnl or 0) > 0)

    @property
    def total_realized_pnl(self) -> float:
        return sum(t.realized_pnl or 0 for t in self.closed_trades)

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def profit_factor(self) -> float:
        gross_profit = sum(t.realized_pnl for t in self.closed_trades if (t.realized_pnl or 0) > 0)
        gross_loss = abs(sum(t.realized_pnl for t in self.closed_trades if (t.realized_pnl or 0) < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 1.0
        return gross_profit / gross_loss

    def unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        total = 0.0
        for pos in self.open_positions:
            price = current_prices.get(pos.pair)
            if price is None:
                continue
            multiplier = 1 if pos.direction == "buy" else -1
            total += (price - pos.entry_price) * pos.position_size * multiplier
        return total


class PositionManager:
    def __init__(self, state_store: StateStore, initial_balance: float, context: str = "PositionManager") -> None:
        self._store = state_store
        self._initial_balance = initial_balance
        self._context = context
        self._load()

    def _load(self) -> None:
        raw_pos = self._store.load_positions_raw()
        self._balance: float = raw_pos.get("account_balance") or self._initial_balance
        self._open: list[Order] = [
            Order.from_dict(p) for p in raw_pos.get("open_positions", [])
        ]
        raw_trades = self._store.load_trades_raw()
        self._closed: list[Order] = [Order.from_dict(t) for t in raw_trades]
        logger.info(
            f"[{self._context}] balance=${self._balance:.2f}, "
            f"open={len(self._open)}, closed={len(self._closed)}"
        )

    def _save(self) -> None:
        self._store.save_positions(
            account_balance=self._balance,
            open_positions=[o.to_dict() for o in self._open],
        )

    def get_account_state(self) -> AccountState:
        return AccountState(
            balance=self._balance,
            initial_balance=self._initial_balance,
            open_positions=list(self._open),
            closed_trades=list(self._closed),
        )

    def get_open_position(self, pair: str) -> Optional[Order]:
        for pos in self._open:
            if pos.pair == pair:
                return pos
        return None

    def open_position(self, order: Order) -> None:
        self._open.append(order)
        self._save()
        logger.info(
            f"[TRADE] Opened {order.direction.upper()} {order.pair} "
            f"@ {order.entry_price:.5f} | SL={order.stop_loss:.5f} TP={order.take_profit:.5f} "
            f"size={order.position_size:.0f}"
        )

    def update_stop_loss(self, order_id: str, new_stop_loss: float) -> bool:
        """オープンポジションのSLを更新する（トレーリングストップ用）。

        SLは利益方向にのみ移動可能。損失方向への移動は無視される。
        """
        for pos in self._open:
            if pos.order_id == order_id:
                if pos.direction == "buy" and new_stop_loss <= pos.stop_loss:
                    return False
                if pos.direction == "sell" and new_stop_loss >= pos.stop_loss:
                    return False
                old_sl = pos.stop_loss
                pos.stop_loss = new_stop_loss
                self._save()
                logger.info(
                    f"[TRAIL] {pos.pair} {pos.direction.upper()} SL updated: "
                    f"{old_sl:.5f} → {new_stop_loss:.5f}"
                )
                return True
        return False

    def close_position(self, order_id: str, close_price: float, reason: str) -> Optional[Order]:
        for i, pos in enumerate(self._open):
            if pos.order_id == order_id:
                multiplier = 1 if pos.direction == "buy" else -1
                # Use Decimal for precision; convert back to float for storage
                pnl = float(
                    (Decimal(str(close_price)) - Decimal(str(pos.entry_price)))
                    * Decimal(str(pos.position_size))
                    * Decimal(multiplier)
                )
                pos.status = "closed"
                pos.closed_at = datetime.now()
                pos.close_price = close_price
                pos.close_reason = reason
                pos.realized_pnl = pnl
                self._balance = float(
                    (Decimal(str(self._balance)) + Decimal(str(pnl))).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                self._open.pop(i)
                self._closed.append(pos)
                self._store.append_trade(pos.to_dict())
                self._save()
                logger.info(
                    f"[TRADE] Closed {pos.direction.upper()} {pos.pair} "
                    f"@ {close_price:.5f} [{reason}] PnL={pnl:+.2f} | "
                    f"balance=${self._balance:.2f}"
                )
                return pos
        logger.warning(f"close_position: order_id {order_id} not found in open positions")
        return None
