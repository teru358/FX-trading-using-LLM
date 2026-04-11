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


@router.post("/ask", dependencies=[Depends(verify_api_key)])
def ask(body: _AskRequest) -> dict[str, Any]:
    """FX 分析 LLM へ質問する。

    soft_timeout 内に完了したら同期返答、超過したら "accepted" + Discord 通知。
    """
    assert state.config is not None and state.llm_slot is not None
    assert state.store is not None and state.analysis_store is not None

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    from src.views import run_ask as _run_ask

    started_at = db_now()
    soft_timeout = state.config.api.ask_soft_timeout_sec

    def _job() -> str:
        return _run_ask(message, state.config, state.store, state.analysis_store)

    job_status, result = state.llm_slot.try_run_user_sync(_job, soft_timeout=soft_timeout)

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

    # promoted or slot_busy → 完了 webhook / background queue
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
