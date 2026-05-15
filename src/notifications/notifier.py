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
    detail_reason: str = ""   # ニュース/テクニカル内訳
    source: str = ""          # "trading" | "forecast" | "monitor" など
    is_scale_in: bool = False # スケールイン注文フラグ


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
class SignalSkippedEvent:
    """シグナルが出たがスキップ/保留された場合（hold含む）。"""
    pair: str
    action: str           # "buy" | "sell" | "hold"
    confidence: float
    signal_reason: str
    detail_reason: str = ""           # ニュース/テクニカル内訳
    predicted_direction: str = ""     # hold時の方向予測
    source: str = ""                  # "trading" | "forecast" | "monitor" など


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


# ── 抽象基底クラス ─────────────────────────────────────────

_SOURCE_LABELS = {
    "trading": "取引サイクル",
    "forecast": "予測サイクル",
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

    async def notify_order_opened(self, event: OrderOpenedEvent) -> None:
        direction_emoji = "📈" if event.direction == "buy" else "📉"
        sl_pips = abs(event.entry_price - event.stop_loss)
        tp_pips = abs(event.take_profit - event.entry_price)
        tag = _source_tag(event.source)
        order_label = "【スケールイン】" if event.is_scale_in else "【注文発注】"
        msg = (
            f"{direction_emoji} {order_label}{tag}{event.pair}\n"
            f"方向: {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f}\n"
            f"SL: {event.stop_loss:.5f}  ({sl_pips:.5f})\n"
            f"TP: {event.take_profit:.5f}  (+{tp_pips:.5f})\n"
            f"サイズ: {event.position_size:,.0f}\n"
            f"確信度: {event.confidence:.0%}"
        )
        if event.detail_reason:
            msg += f"\n─────────────\n{event.detail_reason}"
        else:
            msg += f"\n根拠: {event.signal_reason}"
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
        elif event.close_reason == "profit_lock":
            emoji = "🔐"
            reason_label = "利益確定 (L3 profit_lock)"
        elif event.close_reason == "reversal":
            emoji = "🔁"
            reason_label = "反転シグナル (L1 reversal)"
        elif event.close_reason == "timeout":
            emoji = "⏰"
            reason_label = "保有期間超過 (L2 timeout)"
        elif event.close_reason == "server_sl_tp":
            emoji = "🔄"
            reason_label = "MT5サーバー側決済 (reconciliation検知)"
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

    async def notify_signal_skipped(self, event: SignalSkippedEvent) -> None:
        tag = _source_tag(event.source)
        if event.action == "hold":
            direction_label = event.predicted_direction or "neutral"
            msg = (
                f"⏸️ 【シグナル】{tag}{event.pair} — HOLD ({direction_label}寄り)\n"
                f"確信度: {event.confidence:.0%}"
            )
        else:
            direction_emoji = "📈" if event.action == "buy" else "📉"
            msg = (
                f"{direction_emoji} 【シグナル】{tag}{event.pair} — 既存ポジションのためスキップ\n"
                f"方向: {event.action.upper()}  確信度: {event.confidence:.0%}"
            )
        if event.detail_reason:
            msg += f"\n─────────────\n{event.detail_reason}"
        else:
            msg += f"\n根拠: {event.signal_reason}"
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
