from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── イベント通知データクラス ────────────────────────────────

@dataclass
class OrderOpenedEvent:
    pair: str
    direction: str        # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    confidence: float
    signal_reason: str


@dataclass
class OrderClosedEvent:
    pair: str
    direction: str
    entry_price: float
    close_price: float
    realized_pnl: float
    close_reason: str     # "take_profit" | "stop_loss" | "manual"
    balance: float


@dataclass
class SignalSkippedEvent:
    """シグナルが出たが既存ポジション等でスキップされた場合。"""
    pair: str
    action: str           # "buy" | "sell"
    confidence: float
    signal_reason: str


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


# ── 抽象基底クラス ─────────────────────────────────────────

class NotifierAdapter(ABC):
    @abstractmethod
    async def send(self, message: str) -> None:
        """テキストメッセージを送信する。失敗してもシステムを止めない。"""
        ...

    async def notify_order_opened(self, event: OrderOpenedEvent) -> None:
        direction_emoji = "📈" if event.direction == "buy" else "📉"
        sl_pips = abs(event.entry_price - event.stop_loss)
        tp_pips = abs(event.take_profit - event.entry_price)
        msg = (
            f"{direction_emoji} 【注文発注】{event.pair}\n"
            f"方向: {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f}\n"
            f"SL: {event.stop_loss:.5f}  ({sl_pips:.5f})\n"
            f"TP: {event.take_profit:.5f}  (+{tp_pips:.5f})\n"
            f"サイズ: {event.position_size:,.0f}\n"
            f"確信度: {event.confidence:.0%}\n"
            f"根拠: {event.signal_reason}"
        )
        await self.send(msg)

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
        else:
            emoji = "🔒"
            reason_label = "手動決済"

        pnl_sign = "+" if event.realized_pnl >= 0 else ""
        msg = (
            f"{emoji} 【決済】{event.pair} — {reason_label}\n"
            f"方向: {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f} → {event.close_price:.5f}\n"
            f"損益: {pnl_sign}{event.realized_pnl:.2f}\n"
            f"残高: {event.balance:,.2f}"
        )
        await self.send(msg)

    async def notify_price_alert(self, event: PriceAlertEvent) -> None:
        direction_emoji = "📈" if event.direction == "buy" else "📉"
        pnl_sign = "+" if event.unrealized_pnl >= 0 else ""
        msg = (
            f"⚠️ 【価格急変動】{event.pair}\n"
            f"方向: {direction_emoji} {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f} → 現在: {event.current_price:.5f}\n"
            f"損失: {event.adverse_move_pct:.2%}  未実現損益: {pnl_sign}{event.unrealized_pnl:.2f}\n"
            f"SL: {event.stop_loss:.5f}  SLまで: {event.distance_to_sl_pct:.2%}"
        )
        await self.send(msg)

    async def notify_signal_skipped(self, event: SignalSkippedEvent) -> None:
        direction_emoji = "📈" if event.action == "buy" else "📉"
        msg = (
            f"{direction_emoji} 【シグナル】{event.pair} — 既存ポジションのためスキップ\n"
            f"方向: {event.action.upper()}  確信度: {event.confidence:.0%}\n"
            f"根拠: {event.signal_reason}"
        )
        await self.send(msg)


# ── No-op 実装（通知無効時のデフォルト） ───────────────────

class NullNotifier(NotifierAdapter):
    async def send(self, message: str) -> None:
        pass


# ── ファクトリ ──────────────────────────────────────────────

def create_notifier(notifier_type: str) -> NotifierAdapter:
    """notifier_type に応じた NotifierAdapter を返す。"""
    if notifier_type == "telegram":
        from src.notifications.telegram_notifier import TelegramNotifier
        return TelegramNotifier(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )
    elif notifier_type == "discord":
        from src.notifications.discord_notifier import DiscordNotifier
        return DiscordNotifier(
            webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        )
    elif notifier_type in ("none", "null", ""):
        return NullNotifier()
    else:
        raise ValueError(f"Unknown notifier_type: {notifier_type!r}. Use 'telegram', 'discord', or 'none'.")
