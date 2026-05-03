"""MT5 bridge への管理操作プロキシ (Phase 3b タスク 16)。

discord_bot / CLI が finance API のみを介して bridge の halt / resume を行えるよう、
finance 側でプロキシする。これにより:
- discord_bot は MT5_BRIDGE_URL を持たなくて済む (FINANCE_API_KEY のみで OK)
- finance に admin 操作のログが集約される
- bridge の認証 (X-Bridge-Api-Key) は finance 内部で管理される

bridge は LAN 内のみ接続可能な前提。finance も同 LAN にいるか、stick PC のように
bridge への到達性がある場所で動いている必要がある。
"""
from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api._state import state, verify_api_key

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


class HaltRequest(BaseModel):
    mode: Literal["soft", "hard"] = "soft"
    reason: str = ""


def _bridge_url() -> str:
    """finance config から bridge URL を取得。未設定なら 503。"""
    if state.config is None or not state.config.mt5_bridge.bridge_url:
        raise HTTPException(503, "MT5 bridge not configured (mt5_bridge.bridge_url empty)")
    return state.config.mt5_bridge.bridge_url.rstrip("/")


def _bridge_headers() -> dict[str, str]:
    """bridge への認証ヘッダー (api_key 設定があれば)。"""
    cfg = state.config.mt5_bridge if state.config else None
    api_key = getattr(cfg, "api_key", "") if cfg else ""
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-Bridge-Api-Key"] = api_key
    return h


@router.post("/halt", dependencies=[Depends(verify_api_key)])
def admin_halt(req: HaltRequest) -> dict[str, Any]:
    """bridge を soft / hard halt する。bridge /admin/halt のプロキシ。

    soft: 新規 entry のみ停止、既存ポジ管理は継続。/admin/resume で再開可。
    hard: DRY_RUN 強制 + フラグファイル。**遠隔再開不可**、main PC で手動操作必要。
    """
    url = _bridge_url()
    try:
        resp = httpx.post(
            f"{url}/admin/halt",
            json={"mode": req.mode, "reason": req.reason},
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        logger.warning(f"[ADMIN] proxy halt mode={req.mode} reason={req.reason!r}")
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            e.response.status_code,
            f"bridge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"bridge unreachable: {type(e).__name__}: {e}")


@router.post("/resume", dependencies=[Depends(verify_api_key)])
def admin_resume() -> dict[str, Any]:
    """bridge の soft halt を解除する。bridge /admin/resume のプロキシ。

    hard halt 中は bridge 側が 403 を返し、ここでも 403 を返す (再開には main PC
    での手動操作が必要)。
    """
    url = _bridge_url()
    try:
        resp = httpx.post(
            f"{url}/admin/resume",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        logger.warning("[ADMIN] proxy resume")
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            e.response.status_code,
            f"bridge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"bridge unreachable: {type(e).__name__}: {e}")
