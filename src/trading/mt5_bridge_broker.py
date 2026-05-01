"""mt5_bridge への HTTP クライアント実装の BrokerAdapter。

DRY_RUN は bridge 側のフラグで制御するので、このアダプターは常に同じ呼び出し方を行う。
Phase 3a では `Mt5BridgeBrokerAdapter` 単独でも shadow モード経由でも使える。
"""
from __future__ import annotations

import logging

import httpx

from src.signals.scale_in import PreExecResult, evaluate_pre_execution_checks
from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.paper_trader import check_and_close_positions as _paper_close_check
from src.trading.position_manager import Order, PositionManager
from src.trading.symbol_mapping import to_mt5_symbol

logger = logging.getLogger(__name__)


class Mt5BridgeBrokerAdapter(BrokerAdapter):
    """mt5_bridge (Windows main PC) 経由で MT5 に発注するアダプター。

    Phase 3a では bridge 側 DRY_RUN モードを前提とし、実際の発注は MT5 に届かない。
    `check_and_close_positions` は Phase 3a では paper と同じローカル SL/TP 判定を
    使う (shadow 比較を apples-to-apples にするため)。Phase 3b で MT5 真実
    との reconciliation に置き換える。
    """

    def __init__(
        self,
        bridge_url: str,
        api_key: str = "",
        request_timeout_seconds: float = 10.0,
        lot_size_units: int = 100_000,
        magic_number: int = 12345,
        max_positions_per_pair: int = 2,
        scale_in_enabled: bool = False,
        scale_in_conf_margin: float = 0.05,
        scale_in_score_margin: float = 0.05,
        drawdown_kill_switch_enabled: bool = False,
        drawdown_kill_switch_max_pct: float = 0.10,
        drawdown_kill_switch_lookback_days: int = 0,
    ) -> None:
        if not bridge_url:
            raise ValueError("bridge_url is required")
        self._url = bridge_url.rstrip("/")
        self._api_key = api_key
        self._timeout = request_timeout_seconds
        self._lot_units = lot_size_units
        self._magic = magic_number
        self._max_per_pair = max_positions_per_pair
        self._scale_in_enabled = scale_in_enabled
        self._scale_in_conf_margin = scale_in_conf_margin
        self._scale_in_score_margin = scale_in_score_margin
        self._dd_enabled = drawdown_kill_switch_enabled
        self._dd_max_pct = drawdown_kill_switch_max_pct
        self._dd_lookback = drawdown_kill_switch_lookback_days

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-Bridge-Api-Key"] = self._api_key
        return h

    def execute_signal(
        self, signal: TradeSignal, position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        if signal.action == "hold":
            return None

        direction = "buy" if signal.action == "buy" else "sell"

        # 発注前チェック (ペア上限 / scale-in / drawdown kill switch)
        result = evaluate_pre_execution_checks(
            signal=signal,
            position_mgr=position_mgr,
            max_positions_per_pair=self._max_per_pair,
            scale_in_enabled=self._scale_in_enabled,
            scale_in_conf_margin=self._scale_in_conf_margin,
            scale_in_score_margin=self._scale_in_score_margin,
            drawdown_kill_switch_enabled=self._dd_enabled,
            drawdown_kill_switch_max_pct=self._dd_max_pct,
            drawdown_kill_switch_lookback_days=self._dd_lookback,
        )
        if result.status == "skip":
            logger.info(f"[MT5_BRIDGE] [SKIP] {signal.pair}: {result.reason}")
            return None

        is_scale_in = result.status == "scale_in"

        # 発注 payload (送信時は MT5 形式に変換)
        payload = {
            "symbol": to_mt5_symbol(signal.pair),
            "side": direction,
            "volume_lots": signal.position_size / self._lot_units,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "magic": self._magic,
            "comment": signal.signal_reason[:32],
        }
        try:
            resp = httpx.post(
                f"{self._url}/order", json=payload,
                timeout=self._timeout, headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error(f"[MT5_BRIDGE] bridge order failed: {e}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MT5_BRIDGE] unexpected error: {e}", exc_info=True)
            return None

        # bridge ticket を order_id に組み込む (paper UUID と区別)。
        # pair は内部正規形 (signal.pair = "USDJPY=X") のまま保持。
        order = Order.new(
            pair=signal.pair, direction=direction,
            entry_price=float(data["fill_price"]),
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            position_size=signal.position_size,
            signal_reason=signal.signal_reason,
            macro_context_at_entry=macro_context,
            open_confidence=signal.confidence,
            open_score=signal.combined_score,
            is_scale_in=is_scale_in,
        )
        order.order_id = f"mt5:{data['ticket']}"
        position_mgr.open_position(order)
        ticket = data["ticket"]
        if is_scale_in and result.decision is not None:
            logger.info(
                f"[SCALE] [mt5_bridge] {signal.pair} {direction.upper()} ticket={ticket} | "
                f"new conf={signal.confidence:.3f} score={signal.combined_score:+.3f} | "
                f"prev_max conf={result.decision.prev_max_conf:.3f} "
                f"score={result.decision.prev_max_abs_score:.3f}"
            )
        else:
            logger.info(
                f"[ORDER] [mt5_bridge] {signal.pair} {direction.upper()} ticket={ticket} | "
                f"fill={data['fill_price']} dry_run={data.get('dry_run')}"
            )
        return order

    def check_and_close_positions(
        self, open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        # Phase 3a 簡略化: paper と同じローカル SL/TP 判定を使う。
        # Phase 3b で MT5 真実 (/positions) との reconciliation に置き換える。
        return _paper_close_check(open_positions, current_prices, position_mgr)
