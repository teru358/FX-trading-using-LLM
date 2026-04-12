from __future__ import annotations

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.paper_trader import check_and_close_positions, execute_signal
from src.trading.position_manager import Order, PositionManager


class PaperBrokerAdapter(BrokerAdapter):
    """ローカルシミュレーションによるペーパートレード実装。"""

    def __init__(
        self,
        max_total_positions: int = 4,
        max_positions_per_group: int = 2,
        max_same_direction_per_group: int = 2,
    ) -> None:
        self._max_total = max_total_positions
        self._max_per_group = max_positions_per_group
        self._max_same_dir = max_same_direction_per_group

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        return execute_signal(
            signal, position_mgr, macro_context=macro_context,
            max_total_positions=self._max_total,
            max_positions_per_group=self._max_per_group,
            max_same_direction_per_group=self._max_same_dir,
        )

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        return check_and_close_positions(open_positions, current_prices, position_mgr)
