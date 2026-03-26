"""価格急変動監視ジョブ。

オープンポジションの損失方向への急激な価格変動を定期的に検知し、
閾値超過で通知・緊急損切りを実行する。

_alert_state はプロセス内メモリのみで管理（再起動でリセット）。
同じポジションへの連続通知は alert_step_pct 刻みに抑制する。
"""

from __future__ import annotations

import asyncio
import logging

from src.config import AppConfig
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, PriceAlertEvent, create_notifier
from src.persistence.state_store import StateStore
from src.trading.market_hours import is_market_open
from src.trading.position_manager import PositionManager

logger = logging.getLogger(__name__)

# {order_id: last_alerted_adverse_pct} — 通知スパム防止用（メモリ内のみ）
_alert_state: dict[str, float] = {}


def _apply_trailing_stop(
    pos,
    current: float,
    cfg,
    position_mgr: PositionManager,
) -> None:
    """トレーリングストップを適用する。

    TP方向への到達率が activation_pct を超えたら、
    現在価格から元のSL幅 * distance_ratio だけ離れた位置にSLを追従させる。
    SLは利益方向にのみ移動する（update_stop_loss が保証）。
    """
    tp_distance = abs(pos.take_profit - pos.entry_price)
    if tp_distance == 0:
        return

    if pos.direction == "buy":
        progress = current - pos.entry_price
    else:
        progress = pos.entry_price - current

    progress_pct = progress / tp_distance
    if progress_pct < cfg.trailing_stop_activation_pct:
        return

    # 元のSL距離 × distance_ratio でトレール幅を計算
    original_sl_distance = abs(pos.entry_price - pos.stop_loss)
    trail_distance = original_sl_distance * cfg.trailing_stop_distance_ratio

    if pos.direction == "buy":
        new_sl = current - trail_distance
    else:
        new_sl = current + trail_distance

    position_mgr.update_stop_loss(pos.order_id, round(new_sl, 5))


def _adverse_move_pct(direction: str, entry: float, current: float) -> float:
    """損失方向への移動率を返す（正値 = 損失方向、負値 = 利益方向）。"""
    if direction == "buy":
        return (entry - current) / entry
    else:
        return (current - entry) / entry


async def monitor_open_positions(
    config: AppConfig,
    position_mgr: PositionManager,
) -> None:
    """オープンポジションを確認し、急変動があれば通知・緊急損切りを実行する。"""
    cfg = config.price_monitor
    if not cfg.enabled:
        return

    if not is_market_open():
        return

    account = position_mgr.get_account_state()
    if not account.open_positions:
        return

    notifier = create_notifier(config.notifier.notifier)

    for pos in account.open_positions:
        try:
            current = fetch_current_price(pos.pair)

            # ── トレーリングストップ ──────────────────────────────
            if cfg.trailing_stop_enabled:
                _apply_trailing_stop(pos, current, cfg, position_mgr)

            adverse_pct = _adverse_move_pct(pos.direction, pos.entry_price, current)

            if adverse_pct <= 0:
                # 利益方向に動いている → 通知状態をリセット
                _alert_state.pop(pos.order_id, None)
                continue

            last_alerted = _alert_state.get(pos.order_id, 0.0)
            sl_distance_pct = (
                abs(current - pos.stop_loss) / pos.entry_price
                if pos.stop_loss
                else 0.0
            )
            multiplier = 1 if pos.direction == "buy" else -1
            unrealized_pnl = (current - pos.entry_price) * pos.position_size * multiplier

            # ── 通知チェック ─────────────────────────────────────────
            if (
                config.notifier.notify_on_price_alert
                and adverse_pct >= cfg.alert_threshold_pct
                and adverse_pct >= last_alerted + cfg.alert_step_pct
            ):
                await notifier.notify_price_alert(PriceAlertEvent(
                    pair=pos.pair,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    current_price=current,
                    adverse_move_pct=adverse_pct,
                    stop_loss=pos.stop_loss,
                    distance_to_sl_pct=sl_distance_pct,
                    unrealized_pnl=unrealized_pnl,
                    position_size=pos.position_size,
                ))
                _alert_state[pos.order_id] = adverse_pct
                logger.warning(
                    f"[MONITOR] {pos.pair}: adverse move {adverse_pct:.2%} "
                    f"(entry={pos.entry_price:.5f} current={current:.5f} "
                    f"SL={pos.stop_loss:.5f})"
                )

            # ── 緊急損切りチェック ────────────────────────────────────
            if (
                cfg.enable_emergency_close
                and cfg.emergency_close_pct > 0
                and adverse_pct >= cfg.emergency_close_pct
            ):
                logger.warning(
                    f"[MONITOR] {pos.pair}: emergency close triggered "
                    f"(adverse={adverse_pct:.2%} >= threshold={cfg.emergency_close_pct:.2%})"
                )
                closed = position_mgr.close_position(pos.order_id, current, "emergency_stop")
                _alert_state.pop(pos.order_id, None)

                if closed and config.notifier.notify_on_order_close:
                    account_after = position_mgr.get_account_state()
                    await notifier.notify_order_closed(OrderClosedEvent(
                        pair=closed.pair,
                        direction=closed.direction,
                        entry_price=closed.entry_price,
                        close_price=current,
                        realized_pnl=closed.realized_pnl or 0.0,
                        close_reason="emergency_stop",
                        balance=account_after.balance,
                    ))

        except Exception as e:
            logger.warning(f"[MONITOR] {pos.pair}: price check failed: {e}")


def run_price_monitor(config: AppConfig) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    if not config.price_monitor.enabled:
        return
    if not is_market_open():
        return
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="PriceMonitor")
    if not position_mgr.get_account_state().open_positions:
        return
    asyncio.run(monitor_open_positions(config, position_mgr))
