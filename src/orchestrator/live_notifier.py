"""live 発注結果の Discord 通知 (レビュー High)。

**shadow_notifier.py とは意図的に別モジュール。** live は実弾なので、通知の
有効/無効と宛先が shadow 検証用の設定に巻き込まれてはならない:

  | | 制御フラグ | webhook |
  |---|---|---|
  | live (本モジュール) | ``notification.enabled`` (NotifierConfig) | ``DISCORD_WEBHOOK_URL`` |
  | shadow (shadow_notifier.py) | ``shadow_enabled`` + 各イベントフラグ | ``DISCORD_SHADOW_WEBHOOK_URL`` |

以前は live 通知が ShadowNotifier に同居し ``shadow_triggered`` で gate されていた
ため、(a) shadow 通知 OFF / shadow webhook 未設定で live の約定・拒否・失敗が全て
消え、(b) shadow webhook 設定時は実弾の発注結果が shadow チャンネルへ流れ、
(c) ``notification.enabled`` で live 通知を制御できなかった。

方針は shadow 側と共通:
  - 通知失敗はシステムを止めない (NotifierAdapter.send が内部で握る前提)。
  - Discord 2000 字制限で無言欠落しないよう送信前に切詰める。
  - 🧪 prefix は付けない — shadow 通知と一目で区別できることが最優先。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.notifications.notifier import NotifierAdapter, NullNotifier

if TYPE_CHECKING:
    from src.config.schema import NotifierConfig

logger = logging.getLogger(__name__)

# Discord content の上限は 2000 字。安全マージンを取って分割閾値を置く。
_MAX_CONTENT = 1900


@dataclass
class LiveExecutionInfo:
    """live 発注の結果。

    ``outcome`` は ``ExecutionResult.outcome`` と同じ語彙
    (executed / skipped / halted / rejected / failed)。broker まで届かず
    final gate で弾かれた場合も ``rejected`` として通す (運用者から見ると
    「発注されなかった」点で同じで、理由は reason に載る)。
    """
    pair: str
    action: str             # "buy" | "sell"
    plan_id: int
    outcome: str
    order_id: str | None = None
    reason: str = ""


# live 発注結果の見出し (ExecutionResult.outcome 別)。
# 旧 notifier の _SKIP_HEADLINES と同じ方針: ``skipped`` のみ運用上「無害・想定内」で、
# halted / rejected / failed は要注意であることが文面から分かるようにする。
# **発注に至らなかった outcome の文面に「約定」「executed」等を混ぜないこと** —
# 拒否されたのに発注されたと誤読されるのを防ぐ。
_LIVE_HEADLINES: dict[str, tuple[str, str]] = {
    "executed": ("✅ 発注約定", "約定"),
    "skipped":  ("⏭ 発注スキップ", "発注せず (想定内)"),
    "halted":   ("⛔ halt 中のため発注見送り", "発注せず"),
    "rejected": ("🚫 発注拒否", "発注されていません"),
    "failed":   ("❌ 発注失敗", "発注されていません"),
}


class LiveNotifier:
    """live 発注結果を通常の通知チャンネルへ送る。"""

    def __init__(
        self,
        notifier: NotifierAdapter,
        config: "NotifierConfig",
    ) -> None:
        self._notifier = notifier
        self._config = config

    async def notify_live_execution(self, info: LiveExecutionInfo) -> None:
        """live 発注の結果を通知する。

        約定・拒否・失敗を運用者へ届ける唯一の経路 (Task 8 で旧
        notify_order_opened / notify_signal_skipped を削除して以降)。文面は
        outcome ごとに見出しを変え、**発注に至らなかったケースを「発注された」と
        誤読させない**。
        """
        if not self._config.enabled:
            return
        headline, detail = _LIVE_HEADLINES.get(
            info.outcome, ("❓ 発注結果不明", "outcome"))
        lines = [
            f"{headline} {info.pair} {info.action.upper()}",
            f"plan={info.plan_id}",
        ]
        if info.outcome == "executed" and info.order_id:
            lines[-1] += f" order={info.order_id}"
        else:
            lines[-1] += f" — {detail}"
        if info.reason:
            lines.append(f"reason: {info.reason}")
        await self._send_capped("\n".join(lines))

    async def _send_capped(self, message: str) -> None:
        """Discord 2000 字制限内に収めて送る。

        LLM 由来・broker 由来の reason が長いと 2000 字を超え、Discord が拒否 →
        DiscordNotifier が例外を warning に落とし通知が無言で欠落する。
        """
        if len(message) > _MAX_CONTENT:
            ellipsis = "\n…(truncated)"
            message = message[: _MAX_CONTENT - len(ellipsis)] + ellipsis
        await self._notifier.send(message)


def create_live_notifier(config: "NotifierConfig") -> LiveNotifier:
    """config から LiveNotifier を組み立てる。

    ``notification.enabled`` が false、または ``DISCORD_WEBHOOK_URL`` 未設定なら
    NullNotifier (送信は no-op)。**shadow webhook には決して fallback しない** —
    実弾の発注結果が shadow チャンネルへ流れるのを防ぐ。
    """
    notifier: NotifierAdapter = NullNotifier()
    if config.enabled:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            from src.notifications.discord_notifier import DiscordNotifier

            notifier = DiscordNotifier(webhook_url=webhook)
        else:
            logger.warning(
                "[ORCH] notifications enabled but DISCORD_WEBHOOK_URL not set "
                "— live 発注結果が通知されません (shadow webhook には fallback しない)"
            )
    return LiveNotifier(notifier, config)
