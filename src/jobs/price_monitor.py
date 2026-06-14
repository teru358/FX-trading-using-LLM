"""価格急変動監視ジョブ。

オープンポジションの損失方向への急激な価格変動を定期的に検知し、
閾値超過で通知・緊急損切りを実行する。

_alert_state はプロセス内メモリのみで管理（再起動でリセット）。
同じポジションへの連続通知は alert_step_pct 刻みに抑制する。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.config import AppConfig
from src.data.price_provider import PriceProvider
from src.notifications.notifier import OrderClosedEvent, PriceAlertEvent, create_notifier
from src.persistence.state_store import StateStore
from src.trading.market_hours import is_market_open
from src.trading.position_manager import PositionManager
from src.trading.position_protection import (
    compute_mfe_update,
    compute_profit_protection_action,
    more_protective_sl,
)
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.trading.bridge_health_gate import BridgeHealthGate
    from src.trading.broker_adapter import BrokerAdapter

logger = logging.getLogger(__name__)

# {order_id: last_alerted_adverse_pct} — 通知スパム防止用（メモリ内のみ）
_alert_state: dict[str, float] = {}

# {(pair, hour_key): True} — remote SL sync 失敗時の Discord 通知を時間あたり 1 回に dedup
_remote_sl_alert_dedup: dict[tuple[str, str], bool] = {}


def _hour_key() -> str:
    return db_now().strftime("%Y%m%d%H")


def _is_explicitly_enabled(value) -> bool:
    """Return True only for real bool True; avoids MagicMock defaults in tests."""
    return value is True


def _apply_sl_target(
    pos,
    target_sl: float | None,
    stage: str,
    position_mgr: PositionManager,
    broker: "BrokerAdapter | None",
    remote_sync_enabled: bool,
) -> tuple[bool, bool]:
    """Apply a precomputed SL target with remote-first semantics."""
    if target_sl is None:
        return False, False
    if pos.direction == "buy" and target_sl <= pos.stop_loss:
        return False, False
    if pos.direction == "sell" and target_sl >= pos.stop_loss:
        return False, False
    if remote_sync_enabled and broker is not None:
        if not broker.update_remote_sl(pos.order_id, target_sl):
            return False, True
    updated = position_mgr.update_stop_loss(pos.order_id, target_sl, stage=stage)
    return updated, False


def _apply_profit_protection(
    pos,
    current: float,
    cfg,
    position_mgr: PositionManager,
    broker: "BrokerAdapter | None",
    remote_sync_enabled: bool,
) -> tuple[bool, bool]:
    """Update MFE state and apply R/giveback based SL protection."""
    state = compute_mfe_update(pos, current)
    position_mgr.update_protection_state(
        pos.order_id,
        max_favorable_price=state.max_favorable_price,
        max_favorable_r=state.max_favorable_r,
    )

    action = compute_profit_protection_action(pos, current, cfg)
    action_target = action.target_sl if action.action == "raise_sl" else None
    pending_target = getattr(pos, "pending_protection_sl", None)
    target_sl = more_protective_sl(pos, action_target, pending_target)
    if target_sl is None:
        return False, False

    stage = action.stage if target_sl == action_target else "pending"
    updated, remote_failed = _apply_sl_target(
        pos, target_sl, stage, position_mgr, broker, remote_sync_enabled,
    )
    if updated:
        if pending_target is not None:
            position_mgr.clear_pending_protection_target(pos.order_id)
        position_mgr.update_protection_state(
            pos.order_id,
            max_favorable_price=state.max_favorable_price,
            max_favorable_r=state.max_favorable_r,
            last_protection_stage=stage,
        )
        logger.info(
            f"[POSITION] {pos.pair} r={state.current_r:+.2f} "
            f"mfe_r={state.max_favorable_r:+.2f} giveback_r={state.giveback_r:.2f} "
            f"stage={stage} sl={target_sl:.5f} remote={'ok' if not remote_failed else 'failed'}"
        )
    return updated, remote_failed


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
    broker: "BrokerAdapter",
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
    remote_sync_enabled = config.trading.remote_sl_sync_enabled

    for pos in account.open_positions:
        try:
            current_cp = price_provider.get_current_price(pos.pair, is_monitor=True)
            current = current_cp.price
            mt5_authoritative = current_cp.source == "mt5"

            # ── Profit Protection ─────────────────────────────────────
            remote_failed = False
            if _is_explicitly_enabled(getattr(config.trading, "profit_protection_enabled", False)):
                _, remote_failed = _apply_profit_protection(
                    pos, current, config.trading, position_mgr,
                    broker=broker,
                    remote_sync_enabled=remote_sync_enabled,
                )
            if remote_failed:
                # rate-limited alert: (pair, hour) で 1 回のみ
                key = (pos.pair, _hour_key())
                if key not in _remote_sl_alert_dedup:
                    _remote_sl_alert_dedup[key] = True
                    if config.notifier.notify_on_price_alert:
                        await notifier.notify_price_alert(PriceAlertEvent(
                            pair=pos.pair,
                            direction=pos.direction,
                            entry_price=pos.entry_price,
                            current_price=current,
                            adverse_move_pct=0.0,
                            stop_loss=pos.stop_loss,
                            distance_to_sl_pct=0.0,
                            unrealized_pnl=0.0,
                            position_size=pos.position_size,
                            source="protection_sync_failed",
                        ))

            adverse_pct = _adverse_move_pct(pos.direction, pos.entry_price, current)

            if adverse_pct <= 0:
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
                    f"SL={pos.stop_loss:.5f} src={current_cp.source})"
                )

            # ── 緊急損切りチェック ────────────────────────────────────
            if (
                cfg.enable_emergency_close
                and cfg.emergency_close_pct > 0
                and adverse_pct >= cfg.emergency_close_pct
            ):
                if config.mode in ("live", "live_test") and not mt5_authoritative:
                    # DEGRADED: MT5 価格が取れていない → 誤発火回避、alert のみ
                    logger.warning(
                        f"[MONITOR] {pos.pair}: emergency threshold reached "
                        f"({adverse_pct:.2%}) but price source={current_cp.source} "
                        f"(not MT5); skipping emergency close, sending DEGRADED alert"
                    )
                    if config.notifier.notify_on_price_alert:
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
                            source="monitor_degraded",
                        ))
                    continue

                logger.warning(
                    f"[MONITOR] {pos.pair}: emergency close triggered "
                    f"(adverse={adverse_pct:.2%} >= threshold={cfg.emergency_close_pct:.2%})"
                )
                closed = broker.close_position(pos.order_id, current, "emergency_stop", position_mgr)
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


async def _run_monitor_with_broker(
    config: AppConfig,
    position_mgr: PositionManager,
    price_provider: PriceProvider,
) -> None:
    """broker を構築して monitor_open_positions を呼ぶ内部 async ラッパー。

    asyncio.run に渡す coroutine として使うことで、
    テストが asyncio.run をパッチした場合に build_close_broker も実行されない。
    """
    from src.trading.live_broker import build_close_broker
    broker = build_close_broker(config)
    await monitor_open_positions(config, position_mgr, price_provider, broker)


def run_price_monitor(
    config: AppConfig,
    price_provider: PriceProvider,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。

    gate が渡されたら冒頭で probe する (sync_balance=False、頻度過剰のため)。
    probe 失敗時も monitor は fallback で続行する (TD/yfinance)。
    """
    if not config.price_monitor.enabled:
        return
    if not is_market_open():
        return
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="PriceMonitor")
    if not position_mgr.get_account_state().open_positions:
        return
    if gate is not None:
        gate.probe(caller="monitor", sync_balance=False)
    asyncio.run(_run_monitor_with_broker(config, position_mgr, price_provider))
