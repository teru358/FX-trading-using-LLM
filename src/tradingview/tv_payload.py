"""TradingView Pine Script ペイロード構築の共通ヘルパー。

技術的分析シグナルとオープンポジションを analysis_store/position_mgr から
取得し、SignalData/PositionData に変換して Pine Script を生成する。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from src.tradingview.chart_control import to_tv_ticker
from src.tradingview.script_generator import (
    PositionData,
    SignalData,
    generate_multi_signal_pine,
)

logger = logging.getLogger(__name__)

# ── デバウンス用の状態 (モジュールグローバル) ──
# render_tv_chart は複数の asyncio.run() (trading_cycle / exit_check /
# price_monitor / CLI など) から呼ばれるため、asyncio.Lock は使えない
# (別イベントループに束縛されるとエラー)。タイムスタンプ比較だけを
# threading.Lock で守り、実際の CDP 呼び出し直列化は CDPClient._send_lock に任せる。
_LAST_RENDER_MONOTONIC: float = 0.0
_DEBOUNCE_LOCK = threading.Lock()
_DEBOUNCE_SECONDS: float = 5.0


def _collect_signals(config: Any, analysis_store: Any, hours: int = 8) -> list[SignalData]:
    """trade銘柄の最新スナップショットを SignalData のリストに変換する。"""
    signals: list[SignalData] = []
    for inst in getattr(config, "tradeable_instruments", []):
        try:
            snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=hours)
            if not snaps:
                continue
            snap = snaps[0]
            signals.append(SignalData(
                pair=inst.display_name,
                tv_ticker=to_tv_ticker(inst.symbol),
                direction=getattr(snap, "direction_bias", "hold"),
                entry_price=0.0,  # シグナル段階では約定価格なし
                stop_loss=0.0,
                take_profit=0.0,
                confidence=getattr(snap, "confidence", 0.0),
                reason=(getattr(snap, "reasoning_summary", "") or "")[:80],
                bias_score=getattr(snap, "bias_score", 0.0),
                trend_direction=getattr(snap, "trend_direction", "sideways"),
                key_support=getattr(snap, "key_support", None),
                key_resistance=getattr(snap, "key_resistance", None),
                swing_highs=getattr(snap, "recent_highs", []) or [],
                swing_lows=getattr(snap, "recent_lows", []) or [],
                patterns=", ".join(getattr(snap, "chart_patterns", []) or []),
            ))
        except Exception as e:
            logger.warning(f"[TV] Failed to collect signal for {inst.symbol}: {e}")
    return signals


def _collect_positions(position_mgr: Any) -> list[PositionData]:
    """オープンポジションを PositionData のリストに変換する。"""
    positions: list[PositionData] = []
    try:
        account = position_mgr.get_account_state()
    except Exception as e:
        logger.warning(f"[TV] Failed to fetch account state: {e}")
        return positions

    for order in getattr(account, "open_positions", []):
        try:
            opened_at = order.opened_at
            opened_at_ms = int(opened_at.timestamp() * 1000)
            positions.append(PositionData(
                pair=order.pair,
                tv_ticker=to_tv_ticker(order.pair),
                direction=order.direction,
                entry_price=order.entry_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                position_size=order.position_size,
                opened_at_ms=opened_at_ms,
            ))
        except Exception as e:
            logger.warning(f"[TV] Failed to convert order {getattr(order, 'order_id', '?')}: {e}")
    return positions


def build_tv_pine(config: Any, analysis_store: Any, position_mgr: Any) -> str | None:
    """最新シグナルとオープンポジションから Pine Script を生成する。

    signals / positions が両方空の場合でも、空の Pine Script を返す。
    これによりチャート上の古いインジケーターが上書きクリアされる。
    """
    signals = _collect_signals(config, analysis_store)
    positions = _collect_positions(position_mgr)
    return generate_multi_signal_pine(signals, positions)


async def render_tv_chart(
    config: Any,
    analysis_store: Any,
    position_mgr: Any,
    reason: str = "",
    force: bool = False,
) -> None:
    """TradingView チャートにシグナル + ポジションを再描画する共通ヘルパー。

    ``config.tradingview.enabled`` が False のときは何もしない。
    CDP 接続は (host, port) ごとのプロセス singleton を共有し、毎回 connect/
    disconnect を繰り返さない。送信失敗時は 1 回だけ再接続してリトライする。
    失敗時は警告ログのみで例外は伝播しない。

    デバウンス: 直近 ``_DEBOUNCE_SECONDS`` 秒以内の再描画は skip する
    (trading_cycle と exit_check が同時刻に走るケースなど)。``force=True``
    で明示的に迂回可能 (manual close や CLI 実行など確実に反映したいケース用)。
    """
    if not getattr(config.tradingview, "enabled", False):
        return

    global _LAST_RENDER_MONOTONIC

    with _DEBOUNCE_LOCK:
        now = time.monotonic()
        if not force and (now - _LAST_RENDER_MONOTONIC) < _DEBOUNCE_SECONDS:
            logger.debug(
                f"[TV] render skipped (debounce {_DEBOUNCE_SECONDS:.0f}s) reason={reason}"
            )
            return
        _LAST_RENDER_MONOTONIC = now

    try:
        from src.tradingview.cdp_client import get_shared_cdp_client
        from src.tradingview.pine_injector import PineInjector

        pine = build_tv_pine(config, analysis_store, position_mgr)
        if pine is None:
            return

        tv_cdp = get_shared_cdp_client(config.tradingview.cdp_host, config.tradingview.cdp_port)

        async def _attempt() -> dict | None:
            if not await tv_cdp.ensure_connected():
                return None
            injector = PineInjector(tv_cdp)
            return await injector.inject_and_compile(pine)

        try:
            result = await _attempt()
        except Exception as e:
            logger.info(f"[TV] First attempt failed ({e}); reconnecting and retrying")
            await tv_cdp.disconnect()
            try:
                result = await _attempt()
            except Exception as e2:
                logger.warning(f"[TV] Retry also failed: {e2}")
                return

        if result is None:
            return
        if result.get("success"):
            suffix = f" ({reason})" if reason else ""
            logger.info(f"[TV] Chart updated{suffix}")
        else:
            logger.warning(f"[TV] Pine compile errors: {result.get('errors')}")
    except Exception as e:
        logger.warning(f"[TV] Chart render failed: {e}")
