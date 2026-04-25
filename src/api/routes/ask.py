"""LLM 質問応答エンドポイント。

POST /ask  — FX 分析 LLM へ自然言語で質問する
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api._state import state, verify_api_key
from src.api.notifications import handle_promoted_or_slot_busy, notify_ask_complete
from src.utils.clock import db_now

router = APIRouter()
logger = logging.getLogger(__name__)


class _AskRequest(BaseModel):
    message: str
    # True  : 超過時 "accepted" を返して完了時に Discord へ通知 (Discord bot 発信向け)
    # False : 同期 blocking で完了を待って応答 (CLI client 発信向け)
    # None  : 未指定 → notifier.enabled に追従 (下位互換)
    notify: bool | None = None


# 通知 OFF 時の blocking 待機の最大時間 = ask_soft_timeout_sec × この倍率
# (例: soft=60s × 5 = 300s = 5分まで前のジョブ完了を待つ)
_BLOCKING_WAIT_MULTIPLIER = 5


@router.post("/ask", dependencies=[Depends(verify_api_key)])
def ask(body: _AskRequest) -> dict[str, Any]:
    """FX 分析 LLM へ質問する。

    ``notify`` で通知経路を選ぶ:

    - ``notify=False`` (CLI client): soft_timeout を超えても blocking で待って
      HTTP レスポンスに回答を載せる (Discord には出さない)。
    - ``notify=True`` (Discord bot): soft_timeout 内なら同期返答、超過時は
      "accepted" を返してバックグラウンド継続 + 完了時に Discord webhook で通知。
    - ``notify=None`` (未指定): ``notifier.enabled`` の設定に追従する下位互換動作。
    """
    assert state.config is not None and state.llm_slot is not None
    assert state.store is not None and state.analysis_store is not None

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # notify の解決: 明示 > notifier.enabled (下位互換)
    notify = body.notify if body.notify is not None else state.config.notifier.enabled

    from src.views import run_ask as _run_ask

    started_at = db_now()
    soft_timeout = state.config.api.ask_soft_timeout_sec

    def _job() -> str:
        return _run_ask(message, state.config, state.store, state.analysis_store)

    # ── notify=False: 同期 blocking で最後まで待つ (Discord 通知なし) ────
    if not notify:
        max_wait = soft_timeout * _BLOCKING_WAIT_MULTIPLIER
        job_status, result = state.llm_slot.run_user_blocking_with_timeout(
            _job, timeout=max_wait,
        )
        if job_status == "completed":
            elapsed = (db_now() - started_at).total_seconds()
            return {
                "message": message,
                "response": result,
                "elapsed_seconds": round(elapsed, 1),
                "status": "completed",
            }
        if job_status == "completed_with_error":
            logger.error(f"[API] /ask failed: {result}", exc_info=isinstance(result, BaseException))
            raise HTTPException(status_code=504, detail=f"LLM error: {result}")
        # timeout: 走行中ジョブが max_wait 内に完了しなかった
        raise HTTPException(
            status_code=503,
            detail=(
                f"他のジョブが走行中で {max_wait:.0f}s 以内にスロットを取得できませんでした。"
                f"後ほど再試行してください。"
            ),
        )

    # ── notify=True: soft_timeout 内同期、超過で Discord webhook ────
    job_status, result = state.llm_slot.try_run_user_sync(
        _job,
        soft_timeout=soft_timeout,
        # promoted 完了時に Discord へ回答を通知する (log 頼みにしない)
        on_promoted_complete=lambda r, e: notify_ask_complete(message, r, e, started_at),
    )

    if job_status == "completed":
        elapsed = (db_now() - started_at).total_seconds()
        return {
            "message": message,
            "response": result,
            "elapsed_seconds": round(elapsed, 1),
            "status": "completed",
        }

    if job_status == "completed_with_error":
        logger.error(f"[API] /ask failed: {result}", exc_info=isinstance(result, BaseException))
        raise HTTPException(status_code=504, detail=f"LLM error: {result}")

    # promoted or slot_busy → promoted は on_promoted_complete で通知済み、
    # slot_busy は spawn_user_background で投入 (完了時に notify_ask_complete)
    handle_promoted_or_slot_busy(
        status=job_status,
        job=_job,
        slot=state.llm_slot,
        started_at=started_at,
        job_name="ask",
        on_complete_when_busy=lambda r, e: notify_ask_complete(message, r, e, started_at),
        question=message,
    )

    return {
        "status": "accepted",
        "message": message,
        "notice": "質問を受け付けました。回答が出来次第 Discord へ通知します。",
        "started_at": started_at.isoformat(),
    }
