"""ShadowBrokerAdapter — paper + observer の並走実行 + 比較ログ。

primary (通常 PaperBrokerAdapter) を canonical として実行、
observer (通常 Mt5BridgeBrokerAdapter) を best-effort で並走させる。
observer の結果は JSONL に追記されるだけで、primary の動作・state_store には影響しない。

設計原則:
- primary 失敗時は ShadowBroker も失敗扱い (canonical が失敗したらシャドウは無意味)
- observer 失敗時は警告ログのみ、primary 結果を返す
- observer は専用 PositionManager を inject する想定 (state 分離のため)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter, ExecutionResult
from src.trading.position_manager import Order, PositionManager

logger = logging.getLogger(__name__)


def _serialize_order(o: Order | None) -> dict | None:
    if o is None:
        return None
    return {
        "order_id": o.order_id,
        "pair": o.pair,
        "direction": o.direction,
        "entry_price": o.entry_price,
        "stop_loss": o.stop_loss,
        "take_profit": o.take_profit,
        "position_size": o.position_size,
    }


class ShadowBrokerAdapter(BrokerAdapter):
    def __init__(
        self,
        primary: BrokerAdapter,
        observer: BrokerAdapter,
        observer_position_mgr: PositionManager,
        comparison_log_path: Path | str,
    ) -> None:
        self._primary = primary
        self._observer = observer
        self._obs_pm = observer_position_mgr
        self._log = Path(comparison_log_path)

    def _append(self, record: dict) -> None:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> ExecutionResult:
        primary_result = self._primary.execute_signal(
            signal, position_mgr, macro_context,
        )

        observer_result: ExecutionResult | None = None
        try:
            observer_result = self._observer.execute_signal(
                signal, self._obs_pm, macro_context,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SHADOW] observer execute_signal failed: {e}")

        primary_order = primary_result.order
        observer_order = observer_result.order if observer_result is not None else None
        diff: float | None = None
        if primary_order is not None and observer_order is not None:
            diff = observer_order.entry_price - primary_order.entry_price

        self._append({
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": "execute",
            "signal_pair": signal.pair,
            "signal_action": signal.action,
            "primary": _serialize_order(primary_order),
            "observer": _serialize_order(observer_order),
            "price_diff": diff,
        })
        # canonical は primary。observer は best-effort 記録のみ。
        return primary_result

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        primary_closed = self._primary.check_and_close_positions(
            open_positions, current_prices, position_mgr,
        )
        observer_closed: list[Order] = []
        try:
            observer_open = self._obs_pm.get_account_state().open_positions
            observer_closed = self._observer.check_and_close_positions(
                observer_open, current_prices, self._obs_pm,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SHADOW] observer close check failed: {e}")

        # どちらかでクローズが発生した時のみログ追記 (ノイズ低減)
        if primary_closed or observer_closed:
            self._append({
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "event": "close_check",
                "primary_closed": [_serialize_order(o) for o in primary_closed],
                "observer_closed": [_serialize_order(o) for o in observer_closed],
            })
        return primary_closed
