"""API ジョブ完了通知 + Discord webhook ヘルパー。

``/run/trade`` と ``/ask`` から呼ばれる「promote/slot_busy ブランチを共通化する
完了通知ロジック」を集約する。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time as _time
from datetime import datetime
from typing import Any, Callable

from fastapi import HTTPException

from src.api._state import state
from src.concurrency.priority_job_slot import PriorityJobSlot
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


def send_discord_notification(message: str) -> None:
    """ベストエフォートで Discord webhook を発火する。失敗しても例外を投げない。"""
    try:
        from src.notifications.notifier import create_notifier
        if state.config is None:
            return
        notifier = create_notifier(state.config.notifier.enabled)

        async def _send() -> None:
            await notifier.send(message)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[API] discord notification failed: {e}")


def send_discord_embed(
    title: str,
    description: str,
    color: int = 0x2ecc71,
    footer: str = "",
    fields: list[dict] | None = None,
) -> None:
    """ベストエフォートで Discord webhook embed を発火する。

    長文 (2000 文字超) の回答を送る際は text 版より embed を使う。
    """
    try:
        from src.notifications.notifier import create_notifier
        if state.config is None:
            return
        notifier = create_notifier(state.config.notifier.enabled)

        async def _send() -> None:
            await notifier.send_embed(
                title=title,
                description=description,
                color=color,
                footer=footer,
                fields=fields,
            )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[API] discord embed notification failed: {e}")


def notify_trade_complete(started_at: datetime, error: Exception | None) -> None:
    """spawn_user_background の on_complete 用: 取引サイクル完了を Discord に通知。"""
    elapsed = (db_now() - started_at).total_seconds()
    if error:
        msg = f"❌ 取引サイクル失敗 ({elapsed:.0f}s): {error}"
    else:
        msg = f"✅ 取引サイクル完了 ({elapsed:.0f}s)\n詳細は finance bot ログを参照してください。"
    send_discord_notification(msg)


def notify_ask_complete(
    question: str,
    answer: Any,
    error: Exception | None,
    started_at: datetime,
) -> None:
    """spawn_user_background / on_promoted_complete 用: ask 完了を Discord に通知。

    回答は長文になりがちなので embed で送る (text 版は 2000 文字で切れる)。
    embed の description は 4096 文字まで入るので通常の回答は1つに収まる。
    """
    elapsed = (db_now() - started_at).total_seconds()
    q_short = question[:200] + ("…" if len(question) > 200 else "")

    if error:
        send_discord_embed(
            title=f"❌ ask 失敗 ({elapsed:.0f}s)",
            description=f"**質問**: {q_short}\n\n**エラー**: `{error}`",
            color=0xe74c3c,
        )
        return

    ans_str = str(answer or "").strip() or "(空の応答)"
    # embed description は 4096 文字まで。余裕を持って 4000 で切詰め
    if len(ans_str) > 4000:
        ans_str = ans_str[:4000] + "\n\n…(truncated)"
    send_discord_embed(
        title=f"✅ ask 完了",
        description=ans_str,
        color=0x2ecc71,
        footer=f"{elapsed:.1f}s",
        fields=[{"name": "質問", "value": q_short, "inline": False}],
    )


def schedule_completion_webhook_for_promoted_job(
    slot: PriorityJobSlot,
    job_name: str,
    started_at: datetime,
    question: str | None = None,
) -> None:
    """``try_run_user_sync`` が soft_timeout 超過で promote したジョブの完了を監視し、
    完了時に Discord webhook で通知する。

    ``slot.is_running`` が False になるまでポーリングし、完了を検知して通知する。
    """
    def _wait_and_notify() -> None:
        while slot.is_running:
            _time.sleep(1.0)
        elapsed = (db_now() - started_at).total_seconds()
        if question:
            msg = (
                f"✅ {job_name} 完了 ({elapsed:.0f}s)\n"
                f"**質問**: {question[:200]}\n"
                f"回答は finance bot ログを参照してください。"
            )
        else:
            msg = f"✅ {job_name} 完了 ({elapsed:.0f}s)\n詳細は finance bot ログを参照してください。"
        send_discord_notification(msg)

    threading.Thread(
        target=_wait_and_notify,
        daemon=True,
        name=f"webhook-{job_name}",
    ).start()


def handle_promoted_or_slot_busy(
    status: str,
    job: Callable[[], Any],
    slot: PriorityJobSlot,
    started_at: datetime,
    job_name: str,
    on_complete_when_busy: Callable[[Any, Exception | None], None],
    question: str | None = None,
) -> None:
    """``try_run_user_sync`` の promoted / slot_busy ブランチを共通化する。

    - ``"promoted"``: worker は既に稼働中 → ポーリング通知は不要
       (promoted 時は ``try_run_user_sync`` の ``on_promoted_complete`` 側で直接通知)
    - ``"slot_busy"``: worker 未起動 → ``spawn_user_background`` でキュー投入 (拒否時 409)
    - その他の status は呼び出し側で処理済みの想定なので何もしない
    """
    if status == "promoted":
        # promoted ブランチは on_promoted_complete 側で通知されるため、
        # 別 thread の polling は不要 (従来の schedule_completion_webhook_for_promoted_job は
        # 回答本文を取れなかった問題があった)。ここでは何もしない。
        pass
    elif status == "slot_busy":
        accepted = slot.spawn_user_background(job, on_complete=on_complete_when_busy)
        if not accepted:
            raise HTTPException(
                status_code=409,
                detail="他のユーザージョブが既にキュー待機中です。しばらく経ってから再試行してください。",
            )
