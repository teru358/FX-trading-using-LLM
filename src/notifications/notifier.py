from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from src.trading.position_manager import Order

logger = logging.getLogger(__name__)


# ── イベント通知データクラス ────────────────────────────────

@dataclass
class OrderClosedEvent:
    pair: str
    direction: str
    entry_price: float
    close_price: float
    realized_pnl: float
    close_reason: str     # "take_profit" | "stop_loss" | "manual"
    balance: float
    source: str = ""          # "trading" | "forecast" | "monitor" など


@dataclass
class PriceAlertEvent:
    """損失方向への急激な価格変動を検知した場合。"""
    pair: str
    direction: str        # "buy" | "sell"
    entry_price: float
    current_price: float
    adverse_move_pct: float   # 損失率（正値）
    stop_loss: float
    distance_to_sl_pct: float  # SLまでの残り距離率
    unrealized_pnl: float
    position_size: float
    source: str = ""          # "trading" | "forecast" | "monitor" など


_SOURCE_LABELS = {
    "monitor": "価格監視",
    "exit_check": "決済判定",
    "manual": "手動",
}


def _source_tag(source: str) -> str:
    """source文字列を日本語ラベルに変換し、括弧付きで返す。"""
    if not source:
        return ""
    label = _SOURCE_LABELS.get(source, source)
    return f"[{label}]"


def _pip_size_for(pair: str) -> float:
    """ペア名から pip サイズを返す。

    JPY を quote 側に含むペア (USDJPY=X / EURJPY=X など) は 0.01、
    それ以外 (EURUSD=X / GBPUSD=X など) は 0.0001。FX 業界標準。
    """
    return 0.01 if "JPY" in pair.upper() else 0.0001


class NotifierAdapter(ABC):
    @abstractmethod
    async def send(self, message: str) -> None:
        """テキストメッセージを送信する。失敗してもシステムを止めない。"""
        ...

    async def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x2ecc71,
        footer: str = "",
        fields: list[dict] | None = None,
    ) -> None:
        """構造化された埋め込み (embed) を送信する。

        Discord webhook 等 embed をサポートする実装はリッチな表示を行い、
        それ以外はデフォルト実装でテキスト化して ``send()`` にフォールバックする。

        Args:
            title: 見出し (≤256 chars 推奨)
            description: 本文 (≤4096 chars 推奨、長文 OK)
            color: 16進カラーコード (例: 0x2ecc71)
            footer: フッター文字列
            fields: [{"name": "...", "value": "...", "inline": False}, ...]
        """
        # フォールバック: 連結して send()
        parts: list[str] = [f"**{title}**", description]
        if fields:
            parts.extend(f"**{f.get('name','')}**: {f.get('value','')}" for f in fields)
        if footer:
            parts.append(f"_{footer}_")
        await self.send("\n".join(p for p in parts if p))

    async def notify_order_closed(self, event: OrderClosedEvent) -> None:
        if event.close_reason == "take_profit":
            emoji = "✅"
            reason_label = "TP到達"
        elif event.close_reason == "stop_loss":
            emoji = "🛑"
            reason_label = "SL到達"
        elif event.close_reason == "emergency_stop":
            emoji = "⚠️"
            reason_label = "緊急損切り"
        elif event.close_reason == "profit_lock":
            emoji = "🔐"
            reason_label = "利益確定 (L3 profit_lock)"
        elif event.close_reason == "reversal":
            emoji = "🔁"
            reason_label = "反転シグナル (L1 reversal)"
        elif event.close_reason == "timeout":
            emoji = "⏰"
            reason_label = "保有期間超過 (L2 timeout)"
        elif event.close_reason == "timeout_no_progress":
            emoji = "⏳"
            reason_label = "進捗不足による撤退 (L2 timeout)"
        elif event.close_reason == "timeout_stale_position":
            emoji = "⏰"
            reason_label = "長時間保有レビューによる撤退 (L2 timeout)"
        elif event.close_reason == "server_sl_tp":
            emoji = "🔄"
            reason_label = "MT5サーバー側決済 (reconciliation検知)"
        elif event.close_reason == "server_sl_tp_estimated":
            emoji = "🔄"
            reason_label = "MT5サーバー側決済 (reconciliation検知・損益推定)"
        elif event.close_reason == "manual":
            emoji = "🔒"
            reason_label = "手動決済"
        else:
            emoji = "❓"
            reason_label = f"その他 ({event.close_reason})"

        pnl_sign = "+" if event.realized_pnl >= 0 else ""
        tag = _source_tag(event.source)
        msg = (
            f"{emoji} 【決済】{tag}{event.pair} — {reason_label}\n"
            f"方向: {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f} → {event.close_price:.5f}\n"
            f"損益: {pnl_sign}{event.realized_pnl:.2f}\n"
            f"残高: {event.balance:,.2f}"
        )
        await self.send(msg)

    async def notify_price_alert(self, event: PriceAlertEvent) -> None:
        direction_emoji = "📈" if event.direction == "buy" else "📉"
        pnl_sign = "+" if event.unrealized_pnl >= 0 else ""
        tag = _source_tag(event.source)
        msg = (
            f"⚠️ 【価格急変動】{tag}{event.pair}\n"
            f"方向: {direction_emoji} {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f} → 現在: {event.current_price:.5f}\n"
            f"損失: {event.adverse_move_pct:.2%}  未実現損益: {pnl_sign}{event.unrealized_pnl:.2f}\n"
            f"SL: {event.stop_loss:.5f}  SLまで: {event.distance_to_sl_pct:.2%}"
        )
        await self.send(msg)

# ── No-op 実装（通知無効時のデフォルト） ───────────────────

class NullNotifier(NotifierAdapter):
    async def send(self, message: str) -> None:
        pass


# ── ファクトリ ──────────────────────────────────────────────

def create_notifier(enabled: bool) -> NotifierAdapter:
    """enabled が True なら Discord 通知、False なら NullNotifier を返す。"""
    if not enabled:
        return NullNotifier()
    from src.notifications.discord_notifier import DiscordNotifier
    return DiscordNotifier(
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
    )
