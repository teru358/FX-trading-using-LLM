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
        notifier = create_notifier(state.config.notifier.notifier)

        async def _send() -> None:
            await notifier.send(message)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[API] discord notification failed: {e}")


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
    """spawn_user_background の on_complete 用: ask ジョブ完了を Discord に通知。"""
    elapsed = (db_now() - started_at).total_seconds()
    if error:
        msg = f"❌ ask 失敗 ({elapsed:.0f}s): {error}"
    else:
        ans_str = str(answer or "")
        preview = ans_str[:500] + ("..." if len(ans_str) > 500 else "")
        msg = (
            f"✅ ask 完了 ({elapsed:.0f}s)\n"
            f"**質問**: {question[:200]}\n"
            f"**回答**: {preview}"
        )
    send_discord_notification(msg)


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

    - ``"promoted"``: worker は既に稼働中 → 完了ポーリング + webhook 通知を仕掛ける
    - ``"slot_busy"``: worker 未起動 → ``spawn_user_background`` でキュー投入 (拒否時 409)
    - その他の status は呼び出し側で処理済みの想定なので何もしない
    """
    if status == "promoted":
        schedule_completion_webhook_for_promoted_job(
            slot=slot,
            job_name=job_name,
            started_at=started_at,
            question=question,
        )
    elif status == "slot_busy":
        accepted = slot.spawn_user_background(job, on_complete=on_complete_when_busy)
        if not accepted:
            raise HTTPException(
                status_code=409,
                detail="他のユーザージョブが既にキュー待機中です。しばらく経ってから再試行してください。",
            )
