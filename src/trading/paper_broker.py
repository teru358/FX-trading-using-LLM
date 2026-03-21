from __future__ import annotations

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.paper_trader import check_and_close_positions, execute_signal
from src.trading.position_manager import Order, PositionManager


class PaperBrokerAdapter(BrokerAdapter):
    """ローカルシミュレーションによるペーパートレード実装。"""

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
    ) -> Order | None:
        return execute_signal(signal, position_mgr)

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        return check_and_close_positions(open_positions, current_prices, position_mgr)
