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
        macro_context: str = "",
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

    def close_position(
        self,
        order_id: str,
        close_price: float,
        reason: str,
        position_mgr: PositionManager,
    ) -> Order | None:
        """指定 order_id のポジションを能動的にクローズする (review-based 早期決済用)。

        実取引アダプタ (mt5_bridge / live) はサーバーへ close 指令を送り、確認後に
        内部 state を更新する必要がある。paper / signal アダプタは内部 state 更新だけ
        で済むため、デフォルト実装はそれを行う。

        mt5_bridge のように 「内部 close → 次サイクル reconciliation で MT5 残ポジ
        を orphan 検出 → hard halt」 を防ぐため、サーバー経由が必要なアダプタは
        必ず override する。
        """
        return position_mgr.close_position(order_id, close_price, reason)
