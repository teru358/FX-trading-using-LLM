from __future__ import annotations

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.paper_trader import check_and_close_positions, execute_signal
from src.trading.position_manager import Order, PositionManager


class PaperBrokerAdapter(BrokerAdapter):
    """ローカルシミュレーションによるペーパートレード実装。"""

    def __init__(
        self,
        max_positions_per_pair: int = 2,
        scale_in_enabled: bool = False,
        scale_in_conf_margin: float = 0.05,
        scale_in_score_margin: float = 0.05,
        drawdown_kill_switch_enabled: bool = False,
        drawdown_kill_switch_max_pct: float = 0.10,
    ) -> None:
        self._max_per_pair = max_positions_per_pair
        self._scale_in_enabled = scale_in_enabled
        self._scale_in_conf_margin = scale_in_conf_margin
        self._scale_in_score_margin = scale_in_score_margin
        self._dd_enabled = drawdown_kill_switch_enabled
        self._dd_max_pct = drawdown_kill_switch_max_pct

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        return execute_signal(
            signal, position_mgr, macro_context=macro_context,
            max_positions_per_pair=self._max_per_pair,
            scale_in_enabled=self._scale_in_enabled,
            scale_in_conf_margin=self._scale_in_conf_margin,
            scale_in_score_margin=self._scale_in_score_margin,
            drawdown_kill_switch_enabled=self._dd_enabled,
            drawdown_kill_switch_max_pct=self._dd_max_pct,
        )

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        return check_and_close_positions(open_positions, current_prices, position_mgr)
