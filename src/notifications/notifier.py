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
    """シグナルが出たが発注に至らなかった場合（hold / skip / 拒否 / 失敗）。"""
    pair: str
    action: str           # "buy" | "sell" | "hold"
    confidence: float
    signal_reason: str
    detail_reason: str = ""           # ニュース/テクニカル内訳
    predicted_direction: str = ""     # hold時の方向予測
    source: str = ""                  # "trading" | "forecast" | "monitor" など
    # ExecutionResult の分類 (buy/sell 時の通知文面を決める。hold では無視)。
    outcome: str = "skipped"          # "skipped" | "halted" | "rejected" | "failed"
    skip_reason: str = ""             # 発注に至らなかった実際の理由


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


@dataclass
class SignalOutcome:
    """1シグナルの発注判断結果。集約サマリー整形専用の純粋なデータ構造。"""
    pair: str
    action: str                  # "buy" | "sell" | "hold"
    status: str                  # executed/hold/skipped/halted/rejected/failed
    confidence: float
    combined_score: float
    reason: str                  # executed/hold→signal_reason / それ以外→ExecutionResult.reason
    detail_reason: str           # ニュース/テクニカル詳細内訳
    news_score: float            # signal.news.sentiment_score — drivers 行
    tech_score: float            # signal.price.bias_score — drivers 行
    tv_recommendation: str = ""  # signal.tv_recommendation — drivers 行 ("" なら非表示)
    rag_note: str = ""           # RAG 補正が action/score を変えたときの注記 ("" なら非表示)
    order: Order | None = None   # status=="executed" のとき約定 Order


@dataclass
class CycleSummaryEvent:
    """notify_cycle_summary に渡す、1取引サイクルの集約結果。"""
    cycle_time: datetime
    outcomes: list[SignalOutcome]
    halted: bool = False
    data_health: list[str] = field(default_factory=list)  # 問題文字列。空なら Data 行なし
    source: str = "trading"


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


# skip 通知の見出し (ExecutionResult.outcome 別)。
# ``skipped`` のみ運用上「無害・想定内」。``halted`` / ``rejected`` / ``failed``
# は要注意であることが文面で分かるようにする (発注拒否を無害表示しないため)。
_SKIP_HEADLINES = {
    "skipped":  "スキップ",
    "halted":   "halt 中のため発注見送り",
    "rejected": "🚫 発注拒否",
    "failed":   "❌ 発注失敗",
}


def _format_signal_block(o: SignalOutcome) -> str:
    """1シグナルの結果ブロックを整形する。"""
    if o.status == "executed":
        emoji = "📈" if o.action == "buy" else "📉"
        label = f"{o.action.upper()} EXECUTED"
        if o.order is not None and getattr(o.order, "is_scale_in", False):
            label += " (scale-in)"
    elif o.status == "hold":
        emoji, label = "⏸", "HOLD"
    elif o.status == "rejected":
        emoji, label = "🚫", f"{o.action.upper()} REJECTED"
    elif o.status == "failed":
        emoji, label = "❌", f"{o.action.upper()} FAILED"
    else:  # skipped / halted
        emoji = "⏭"
        label = f"{o.action.upper()} SKIPPED" if o.action in ("buy", "sell") else "SKIPPED"

    lines = [f"{emoji} {o.pair} {label}"]

    score_line = f"score {o.combined_score:+.3f} | conf {o.confidence:.0%}"
    if o.status == "executed" and o.order is not None:
        entry, sl, tp = o.order.entry_price, o.order.stop_loss, o.order.take_profit
        sl_dist = abs(entry - sl)
        rr = abs(tp - entry) / sl_dist if sl_dist > 0 else 0.0
        score_line += f" | RR {rr:.2f}"
    lines.append(score_line)

    if o.status == "executed" and o.order is not None:
        lines.append(
            f"entry {o.order.entry_price:.5f} | "
            f"SL {o.order.stop_loss:.5f} | TP {o.order.take_profit:.5f}"
        )

    if o.status in ("executed", "hold"):
        drivers = f"drivers: News {o.news_score:+.2f} / Tech {o.tech_score:+.2f}"
        if o.tv_recommendation:
            drivers += f" / TV {o.tv_recommendation}"
        lines.append(drivers)

    if o.reason:
        lines.append(f"reason: {o.reason}")
    if o.rag_note:
        lines.append(f"RAG: {o.rag_note}")

    return "\n".join(lines)


def _format_cycle_summary(event: CycleSummaryEvent) -> str:
    """1取引サイクルの集約サマリーのメッセージ文字列を組み立てる。"""
    hhmm = event.cycle_time.strftime("%H:%M")

    if event.halted:
        return (
            f"🛑 取引サイクル {hhmm} JST\n"
            "halt 中 — 新規発注分析をスキップ\n"
            "既存ポジション管理 (timeout 判定) のみ継続"
        )

    n_exec = sum(1 for o in event.outcomes if o.status == "executed")
    n_hold = sum(1 for o in event.outcomes if o.status == "hold")
    n_rej = sum(1 for o in event.outcomes if o.status == "rejected")
    n_fail = sum(1 for o in event.outcomes if o.status == "failed")
    n_skip = sum(1 for o in event.outcomes if o.status in ("skipped", "halted"))

    has_problem = n_rej > 0 or n_fail > 0 or bool(event.data_health)
    header_emoji = "⚠️" if has_problem else "🟢"

    counts = f"{n_exec}発注 / {n_hold}HOLD / {n_rej}拒否 / {n_fail}失敗"
    if n_skip > 0:
        counts += f" / {n_skip}スキップ"

    lines = [f"{header_emoji} 取引サイクル {hhmm} JST", f"結果: {counts}"]
    if event.data_health:
        lines.append("⚠ Data: " + " / ".join(event.data_health))
    for o in event.outcomes:
        lines.append("")
        lines.append(_format_signal_block(o))

    msg = "\n".join(lines)
    if len(msg) > 1900:  # Discord content 上限 2000 字に対する安全マージン
        msg = msg[:1900] + "\n…(以下省略)"
    return msg


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
            headline = _SKIP_HEADLINES.get(event.outcome, "発注見送り")
            msg = (
                f"{direction_emoji} 【シグナル】{tag}{event.pair} — {headline}\n"
                f"方向: {event.action.upper()}  確信度: {event.confidence:.0%}"
            )
            if event.skip_reason:
                msg += f"\n理由: {event.skip_reason}"
        if event.detail_reason:
            msg += f"\n─────────────\n{event.detail_reason}"
        else:
            msg += f"\n根拠: {event.signal_reason}"
        await self.send(msg)

    async def notify_cycle_summary(self, event: CycleSummaryEvent) -> None:
        """取引サイクルの集約サマリーを送信する。"""
        await self.send(_format_cycle_summary(event))


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
