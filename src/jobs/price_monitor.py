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
from src.data.price_provider import PriceProvider
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
) -> bool:
    """段階型トレーリングストップを適用する。

    戻り値: SL が実際に更新された場合 True (update_stop_loss が True を返した場合のみ)。
    それ以外 (進捗未達、片方向ガード拒否、条件不成立) は False。

    進捗率(TP方向への到達率)に応じてSLを段階的に繰り上げる:
    - `breakeven_pct / 2` 以上: SL = entry と 元SL の中間 (半額ロック, stage="half")
    - `breakeven_pct` 以上:     SL = entry (損益ゼロ, stage="breakeven")
    - `activation_pct` 以上:    SL = current − 元SL距離 × distance_ratio (動的追従, stage="follow")
    """
    if not pos.take_profit:
        return False

    tp_distance = abs(pos.take_profit - pos.entry_price)
    if tp_distance == 0:
        return False

    if pos.direction == "buy":
        progress = current - pos.entry_price
    else:
        progress = pos.entry_price - current

    progress_pct = progress / tp_distance
    if progress_pct <= 0:
        return False

    breakeven_pct = cfg.trailing_stop_breakeven_pct
    half_pct = breakeven_pct / 2.0
    activation_pct = cfg.trailing_stop_activation_pct

    # 元SL距離は Order.initial_stop_loss を基準に計算する（break-even後も不変）
    # initial_stop_loss == 0.0 は「未設定」のセンチネル
    initial_sl = pos.initial_stop_loss if pos.initial_stop_loss != 0.0 else pos.stop_loss
    original_sl_distance = abs(pos.entry_price - initial_sl)
    if original_sl_distance == 0:
        return False

    # 動的追従ステージ
    if progress_pct >= activation_pct:
        trail_distance = original_sl_distance * cfg.trailing_stop_distance_ratio
        if pos.direction == "buy":
            new_sl = current - trail_distance
        else:
            new_sl = current + trail_distance
        return position_mgr.update_stop_loss(pos.order_id, round(new_sl, 5), stage="follow")

    # break-even ステージ
    if progress_pct >= breakeven_pct:
        return position_mgr.update_stop_loss(pos.order_id, round(pos.entry_price, 5), stage="breakeven")

    # 半額ステージ
    if progress_pct >= half_pct:
        midpoint = (pos.entry_price + initial_sl) / 2.0
        return position_mgr.update_stop_loss(pos.order_id, round(midpoint, 5), stage="half")

    return False


def _adverse_move_pct(direction: str, entry: float, current: float) -> float:
    """損失方向への移動率を返す（正値 = 損失方向、負値 = 利益方向）。"""
    if direction == "buy":
        return (entry - current) / entry
    else:
        return (current - entry) / entry


async def monitor_open_positions(
    config: AppConfig,
    position_mgr: PositionManager,
    price_provider: PriceProvider,
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

    notifier = create_notifier(config.notifier.enabled)

    for pos in account.open_positions:
        try:
            current = price_provider.get_current_price(pos.pair, is_monitor=True).price

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
                    source="monitor",
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
                        source="monitor",
                    ))

        except Exception as e:
            logger.warning(f"[MONITOR] {pos.pair}: price check failed: {e}")


def run_price_monitor(config: AppConfig, price_provider: PriceProvider) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    if not config.price_monitor.enabled:
        return
    if not is_market_open():
        return
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="PriceMonitor")
    if not position_mgr.get_account_state().open_positions:
        return
    asyncio.run(monitor_open_positions(config, position_mgr, price_provider))
