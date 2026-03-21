from __future__ import annotations

from abc import ABC, abstractmethod

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order, PositionManager


class BrokerAdapter(ABC):
    """取引執行の抽象インターフェース。ペーパートレード・本取引を共通APIで扱う。"""

    @abstractmethod
    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
    ) -> Order | None:
        """シグナルに基づき注文を発注する。ポジション済みの場合は None を返す。"""
        ...

    @abstractmethod
    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        """SL/TP到達ポジションをクローズし、クローズ済み Order リストを返す。"""
        ...
