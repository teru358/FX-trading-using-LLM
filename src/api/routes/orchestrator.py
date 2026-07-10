"""orchestrator plan 系 endpoint (approval gate spec F-5)。

権威は finance 側 — discord_bot は UI アダプタとしてここを呼ぶ。
認証は既存 X-API-Key (verify_api_key)。store は APIState.orchestrator_store
(start_api_server で注入。未注入は 503 — headless 構成の保護)。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from src.api._state import state, verify_api_key
from src.data.orchestrator_store import PLAN_STATUSES
from src.orchestrator.plan_view import plan_to_row

router = APIRouter()

# SQLite INTEGER max (signed 64bit). plan_id は正の autoincrement のため ge=1。
_PLAN_ID = Path(..., ge=1, le=2**63 - 1)


def _store():
    store = state.orchestrator_store
    if store is None:
        raise HTTPException(status_code=503, detail="orchestrator store not configured")
    return store


def _row(store, plan) -> dict[str, Any]:
    row = plan_to_row(
        plan, reasoning=store.get_latest_plan_create_reasoning(plan.plan_id)
    )
    row["status"] = plan.status
    row["gate_decision"] = plan.gate_decision
    row["gate_message_id"] = plan.gate_message_id
    return row


@router.get("/orchestrator/plans", dependencies=[Depends(verify_api_key)])
def list_plans(
    status: str = "pending_approval",
    # 負値・巨大値は timedelta(hours=...) で OverflowError→500 を起こすため境界化
    # (実装後レビュー Low)。720h = 30日を上限とする。
    posted_within_hours: int | None = Query(default=None, ge=1, le=720),
) -> dict[str, Any]:
    """plan 一覧。posted_within_hours 指定時は reconcile モード (status 不問・
    gate_message_id あり・updated_at 窓 — bot 再起動復旧用)。"""
    store = _store()
    if posted_within_hours is not None:
        plans = store.get_gate_posted_plans(within_hours=posted_within_hours)
    else:
        # 未知 status を空リストで黙って返さず 422 (API 衛生・final review Minor)。
        if status not in PLAN_STATUSES:
            raise HTTPException(status_code=422, detail=f"invalid status: {status!r}")
        plans = store.get_plans_by_status((status,))
    return {"plans": [_row(store, p) for p in plans]}


@router.get("/orchestrator/plans/{plan_id}", dependencies=[Depends(verify_api_key)])
def plan_detail(plan_id: int = _PLAN_ID) -> dict[str, Any]:
    """plan 詳細 — polling で pending から消えた plan の結末判定 (bot の edit 用)。"""
    store = _store()
    plan = store.get_trade_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    row = _row(store, plan)
    row["gate_decided_at"] = plan.gate_decided_at
    row["gate_reason"] = plan.gate_reason
    return row


class _RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class _GateMessageBody(BaseModel):
    message_id: str


@router.post(
    "/orchestrator/plans/{plan_id}/approve",
    dependencies=[Depends(verify_api_key)],
)
def approve_plan(plan_id: int = _PLAN_ID) -> dict[str, Any]:
    store = _store()
    if store.get_trade_plan(plan_id) is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if not store.try_decide_gate(plan_id, "approved"):
        raise HTTPException(status_code=409, detail="plan is not pending_approval")
    return {"plan_id": plan_id, "status": "active"}


@router.post(
    "/orchestrator/plans/{plan_id}/reject",
    dependencies=[Depends(verify_api_key)],
)
def reject_plan(
    plan_id: int = _PLAN_ID, body: _RejectBody | None = None
) -> dict[str, Any]:
    store = _store()
    if store.get_trade_plan(plan_id) is None:
        raise HTTPException(status_code=404, detail="plan not found")
    reason = body.reason if body is not None else None
    if not store.try_decide_gate(plan_id, "rejected", reason=reason):
        raise HTTPException(status_code=409, detail="plan is not pending_approval")
    return {"plan_id": plan_id, "status": "rejected"}


@router.post(
    "/orchestrator/plans/{plan_id}/gate_message",
    dependencies=[Depends(verify_api_key)],
)
def set_gate_message(
    plan_id: int = _PLAN_ID, body: _GateMessageBody = ...
) -> dict[str, Any]:
    """bot が Discord 投稿直後に呼ぶ (再起動突合の正本を finance 側へ)。冪等。"""
    if not _store().set_gate_message(plan_id, body.message_id):
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan_id": plan_id, "gate_message_id": body.message_id}
