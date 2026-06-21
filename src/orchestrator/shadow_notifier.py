"""shadow 専用 Discord 通知 (Phase 5 / design §11)。

shadow 検証 (Phase 2) の plan ライフサイクル / shadow trigger / hindsight 評価 /
daily summary を、既存 cycle summary とは**別 channel**へ通知する。混線を避けるため
webhook は `DISCORD_SHADOW_WEBHOOK_URL` を優先し、未設定なら既存 `DISCORD_WEBHOOK_URL`
へフォールバックする (factory)。全メッセージは 🧪 prefix を付け、shadow である旨が
一目で分かるようにする。

設計境界:
  - 既存 `notifier.py` の `CycleSummaryEvent` / `notify_cycle_summary` 経路は使わない
    (混ざらないことの担保)。shadow は本モジュール内で文字列を組み send() に直書きする。
  - 通知失敗はシステムを止めない (NotifierAdapter.send が内部で握る前提)。
  - 各イベントは config フラグで個別 on/off。`shadow_enabled` がマスタースイッチ。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from src.notifications.notifier import NotifierAdapter, NullNotifier

if TYPE_CHECKING:
    from src.config.schema import OrchestratorNotificationsConfig
    from src.orchestrator.shadow_metrics import ShadowMetrics

logger = logging.getLogger(__name__)

# Discord content の上限は 2000 字。安全マージンを取って分割閾値を置く。
_MAX_CONTENT = 1900

_PREFIX = "🧪"


# ── イベント入力データ (純粋なデータ構造) ──────────────────────


@dataclass
class PlanCreatedInfo:
    pair: str
    direction: str          # "long" | "short"
    plan_id: int
    score: float | None
    confidence: float | None
    reason: str = ""


@dataclass
class ShadowTriggerInfo:
    pair: str
    direction: str          # "long" | "short"
    plan_id: int
    score: float | None
    confidence: float | None
    trigger_price: float
    sl: float | None
    tp: float | None
    rr: float | None
    reason: str = ""


@dataclass
class HindsightInfo:
    pair: str
    direction: str
    plan_id: int | None
    mfe_r: float | None
    mae_r: float | None
    pnl_r: float | None
    would_hit_tp: bool | None = None
    would_hit_sl: bool | None = None


# ── 整形ヘルパ ─────────────────────────────────────────────


def _fmt_opt(value: float | None, spec: str = "+.2f") -> str:
    return format(value, spec) if value is not None else "—"


class ShadowNotifier:
    """shadow イベントを 🧪 prefix 付きで shadow channel へ送る。"""

    def __init__(
        self,
        notifier: NotifierAdapter,
        config: "OrchestratorNotificationsConfig",
    ) -> None:
        self._notifier = notifier
        self._config = config

    # ── plan ライフサイクル (shadow_plan_created で一括 gate) ──────

    async def notify_plan_created(self, info: PlanCreatedInfo) -> None:
        if not self._enabled(self._config.shadow_plan_created):
            return
        msg = (
            f"{_PREFIX} Plan created {info.pair} {info.direction.upper()}\n"
            f"plan={info.plan_id} score={_fmt_opt(info.score, '.2f')} "
            f"conf={_fmt_opt(info.confidence, '.2f')}"
        )
        if info.reason:
            msg += f"\nreason: {info.reason}"
        await self._send_capped(msg)

    async def notify_plan_rejected(self, *, pair: str, reason: str) -> None:
        if not self._enabled(self._config.shadow_plan_created):
            return
        await self._send_capped(
            f"{_PREFIX} Plan rejected {pair}\nreason: {reason}"
        )

    async def notify_plan_superseded(
        self, *, pair: str, old_plan_id: int, new_plan_id: int
    ) -> None:
        if not self._enabled(self._config.shadow_plan_created):
            return
        await self._send_capped(
            f"{_PREFIX} Plan superseded {pair}\n"
            f"plan={old_plan_id} → plan={new_plan_id}"
        )

    # ── shadow trigger ────────────────────────────────────────

    async def notify_shadow_trigger(self, info: ShadowTriggerInfo) -> None:
        if not self._enabled(self._config.shadow_triggered):
            return
        lines = [
            f"{_PREFIX} Shadow trigger {info.pair} {info.direction.upper()}",
            f"plan={info.plan_id} score={_fmt_opt(info.score, '.2f')} "
            f"conf={_fmt_opt(info.confidence, '.2f')}",
        ]
        levels = f"trigger {info.trigger_price:.3f}"
        if info.sl is not None:
            levels += f" / SL {info.sl:.3f}"
        if info.tp is not None:
            levels += f" / TP {info.tp:.3f}"
        if info.rr is not None:
            levels += f" / RR {info.rr:.1f}"
        lines.append(levels)
        if info.reason:
            lines.append(f"reason: {info.reason}")
        await self._send_capped("\n".join(lines))

    # ── hindsight ─────────────────────────────────────────────

    async def notify_hindsight_evaluated(self, info: HindsightInfo) -> None:
        if not self._enabled(self._config.shadow_hindsight):
            return
        hit = ""
        if info.would_hit_tp:
            hit = " (TP hit)"
        elif info.would_hit_sl:
            hit = " (SL hit)"
        plan_part = f"plan={info.plan_id} " if info.plan_id is not None else ""
        await self._send_capped(
            f"{_PREFIX} Hindsight {info.pair} {info.direction.upper()}{hit}\n"
            f"{plan_part}PnL {_fmt_opt(info.pnl_r)}R | "
            f"MFE {_fmt_opt(info.mfe_r)}R | MAE {_fmt_opt(info.mae_r)}R"
        )

    # ── daily summary ─────────────────────────────────────────

    async def notify_daily_summary(
        self, metrics: "ShadowMetrics", *, day: datetime
    ) -> None:
        if not self._enabled(self._config.shadow_daily_summary):
            return
        header = f"{_PREFIX} Shadow daily summary {day.strftime('%Y-%m-%d')}"
        lines = [
            f"plans: created={metrics.plans_created} triggered={metrics.plans_triggered} "
            f"invalidated={metrics.plans_invalidated} expired={metrics.plans_expired} "
            f"superseded={metrics.plans_superseded}",
            f"trigger rate: {metrics.trigger_rate:.0%}",
            f"hindsight: evaluated={metrics.hindsight_evaluated} "
            f"TP {metrics.tp_hit_rate:.0%} / SL {metrics.sl_hit_rate:.0%}",
            f"avg PnL {_fmt_opt(metrics.avg_pnl_r)}R | "
            f"MFE {_fmt_opt(metrics.avg_mfe_r)}R | MAE {_fmt_opt(metrics.avg_mae_r)}R",
            f"LLM failure rate: {metrics.llm_failure_rate:.0%} "
            f"({metrics.agent_runs_failed}/{metrics.agent_runs_total})",
            f"freshness blocks: {metrics.freshness_blocks}",
        ]
        await self._send_chunked(header, lines)

    # ── 内部 ──────────────────────────────────────────────────

    def _enabled(self, event_flag: bool) -> bool:
        """マスタースイッチ AND イベント個別フラグ。"""
        return self._config.shadow_enabled and event_flag

    async def _send_capped(self, message: str) -> None:
        """単発メッセージを Discord 2000 字制限内に収めて送る (Codex Low-Medium#4)。

        LLM 由来の reason が長いと 2000 字を超え、Discord が拒否 → DiscordNotifier が
        例外を warning に落とし通知が無言で欠落する。送信前に末尾を切詰めて欠落を防ぐ。
        """
        if len(message) > _MAX_CONTENT:
            ellipsis = "\n…(truncated)"
            message = message[: _MAX_CONTENT - len(ellipsis)] + ellipsis
        await self._notifier.send(message)

    async def _send_chunked(self, header: str, lines: list[str]) -> None:
        """header + 複数行を 2000 字制限内のメッセージに分割して送る。

        1 行が単体で上限を超える病的ケースは行単位でそのまま送る (途中切断を避ける)。
        各チャンクは header を先頭に持ち、どのメッセージも自己完結させる。
        """
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = len(header)
        for line in lines:
            add_len = len(line) + 1  # 改行込み
            if current and current_len + add_len > _MAX_CONTENT:
                chunks.append(current)
                current = []
                current_len = len(header)
            current.append(line)
            current_len += add_len
        if current:
            chunks.append(current)
        if not chunks:
            await self._notifier.send(header)
            return
        for chunk in chunks:
            await self._notifier.send("\n".join([header, *chunk]))


# ── factory ──────────────────────────────────────────────────


def create_shadow_notifier(
    config: "OrchestratorNotificationsConfig",
) -> ShadowNotifier:
    """config から ShadowNotifier を組み立てる。

    `shadow_enabled` が false、または `DISCORD_SHADOW_WEBHOOK_URL` 未設定なら
    NullNotifier を使う (送信は no-op)。

    **本番 `DISCORD_WEBHOOK_URL` には fallback しない (Codex Medium#2)。** fallback すると
    shadow 通知が本番 (取引サイクル) チャンネルに漏れ、Phase 5 の「shadow-only・既存と
    分離」という目的が崩れる。shadow 通知を出したい場合は専用 webhook を明示設定する。
    """
    notifier: NotifierAdapter = NullNotifier()
    if config.shadow_enabled:
        webhook = os.environ.get("DISCORD_SHADOW_WEBHOOK_URL", "")
        if webhook:
            from src.notifications.discord_notifier import DiscordNotifier

            notifier = DiscordNotifier(webhook_url=webhook)
        else:
            logger.warning(
                "[ORCH] shadow notifications enabled but DISCORD_SHADOW_WEBHOOK_URL "
                "not set — using NullNotifier (本番 channel には fallback しない)"
            )
    return ShadowNotifier(notifier, config)
