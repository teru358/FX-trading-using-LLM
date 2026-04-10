"""TradingView Pine Script ペイロード構築の共通ヘルパー。

技術的分析シグナルとオープンポジションを analysis_store/position_mgr から
取得し、SignalData/PositionData に変換して Pine Script を生成する。
"""
from __future__ import annotations

import logging
from typing import Any

from src.tradingview.chart_control import to_tv_ticker
from src.tradingview.script_generator import (
    PositionData,
    SignalData,
    generate_multi_signal_pine,
)

logger = logging.getLogger(__name__)


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

    どちらも空の場合は None を返す (無駄な注入を防ぐ)。
    """
    signals = _collect_signals(config, analysis_store)
    positions = _collect_positions(position_mgr)

    if not signals and not positions:
        return None

    return generate_multi_signal_pine(signals, positions)
