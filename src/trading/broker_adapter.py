from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order, PositionManager

ExecutionOutcome = Literal["executed", "skipped", "halted", "rejected", "failed"]


@dataclass(frozen=True)
class ExecutionResult:
    """``execute_signal`` の結果。``outcome`` で成否と理由分類を表す。

    - ``executed``: 発注成功 (``order`` に約定 ``Order``)
    - ``skipped`` : 意図した抑制 (既存ポジション / scale-in 無効 / リスク制限 / hold)
    - ``halted``  : halt 状態のため発注見送り
    - ``rejected``: bridge が拒否ステータス (HTTP 409 / 422) を返した
                    — invalid stops, 証拠金不足など
    - ``failed``  : 技術的失敗 (bridge 不通 / HTTP 5xx / 例外)

    ``skipped`` のみ運用上「無害・想定内」。``halted`` / ``rejected`` / ``failed``
    は要注意であり、通知でもそれが分かる文面にする (バグ2 の再発防止)。

    分類は bridge が返す HTTP ステータスに従う。MT5 が拒否した注文でも bridge が
    5xx で返す場合 (例: invalid comment を HTTP 500 で返す現状) は ``failed`` に
    落ちる。bridge が invalid order で一貫して 4xx を返すよう直せば自動的に
    ``rejected`` へ分類される (インシデント3 の対応範囲)。
    """

    outcome: ExecutionOutcome
    order: Order | None = None
    reason: str = ""

    @property
    def is_executed(self) -> bool:
        return self.outcome == "executed"

    @classmethod
    def executed(cls, order: Order) -> "ExecutionResult":
        return cls("executed", order=order)

    @classmethod
    def skipped(cls, reason: str) -> "ExecutionResult":
        return cls("skipped", reason=reason)

    @classmethod
    def halted(cls, reason: str) -> "ExecutionResult":
        return cls("halted", reason=reason)

    @classmethod
    def rejected(cls, reason: str) -> "ExecutionResult":
        return cls("rejected", reason=reason)

    @classmethod
    def failed(cls, reason: str) -> "ExecutionResult":
        return cls("failed", reason=reason)


class BrokerAdapter(ABC):
    """取引執行の抽象インターフェース。ペーパートレード・本取引を共通APIで扱う。"""

    @abstractmethod
    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> ExecutionResult:
        """シグナルに基づき注文を発注し、結果を ``ExecutionResult`` で返す。

        成功時は ``outcome="executed"`` + ``order``。発注に至らない/失敗した場合は
        ``skipped`` / ``halted`` / ``rejected`` / ``failed`` のいずれかと理由を返す。
        """
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

    def update_remote_sl(self, order_id: str, new_sl: float) -> bool:
        """remote broker 側の SL を更新する (Layer 4 trailing stop 同期用)。

        paper / shadow / live (OANDA-未実装) は no-op で True を返す。
        mt5_bridge は POST /positions/{ticket}/modify を呼ぶ。

        Args:
            order_id: 内部 order_id (mt5:<ticket> または paper UUID)
            new_sl: 新しい SL 値

        Returns:
            成功 True / 失敗 False。失敗時は呼出側で WARN ログ + 次 cycle 再送 (idempotent)。
        """
        return True
