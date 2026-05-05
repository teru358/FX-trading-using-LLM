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
    if state.config is None:
        raise HTTPException(503, "config not loaded")
    mt5_cfg = state.config.providers.mt5
    if mt5_cfg is None or not mt5_cfg.bridge_url:
        raise HTTPException(
            503,
            "MT5 bridge not configured (live_broker != 'mt5' or providers/mt5.yaml missing)",
        )
    return mt5_cfg.bridge_url.rstrip("/")


def _bridge_headers() -> dict[str, str]:
    """bridge への認証ヘッダー (api_key 設定があれば)。"""
    h = {"Content-Type": "application/json"}
    if state.config is None:
        return h
    mt5_cfg = state.config.providers.mt5
    if mt5_cfg is not None and mt5_cfg.api_key:
        h["X-Bridge-Api-Key"] = mt5_cfg.api_key
    return h


@router.post("/halt", dependencies=[Depends(verify_api_key)])
def admin_halt(req: HaltRequest) -> dict[str, Any]:
    """bridge を soft / hard halt する。

    soft: finance halt.json を権威的に更新し、bridge へは best-effort で伝搬。
          bridge 不通でも finance halt は確定する (Discord 通知は admin 経路で
          は出さない — manual 操作はユーザーが既に意図しているため)。
    hard: 既存通り bridge へのプロキシのみ (本仕様 out of scope)。
    """
    if state.config is not None and state.config.mode == "paper":
        raise HTTPException(
            status_code=400,
            detail="halt is not available in paper mode (no bridge to halt)",
        )

    # mode=hard は out of scope。既存挙動 (proxy only) を維持。
    if req.mode == "hard":
        return _proxy_bridge_halt(req)

    # mode=soft: finance state を権威的に更新
    from src.persistence import halt_state
    assert state.config is not None
    new_state, _changed = halt_state.trigger_manual(
        state.config.state_dir, reason=req.reason or "manual halt"
    )
    logger.warning(
        f"[ADMIN] manual halt finance state set "
        f"(soft_halted={new_state.soft_halted}, reason={req.reason!r})"
    )

    # bridge POST best-effort
    bridge_url = _bridge_url()
    bridge_response: dict[str, Any]
    try:
        resp = httpx.post(
            f"{bridge_url}/admin/halt",
            json={"mode": "soft", "reason": req.reason},
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        bridge_response = resp.json()
    except httpx.HTTPError as e:
        logger.warning(
            f"[ADMIN] bridge halt POST failed (finance state set): {e}"
        )
        bridge_response = {"error": f"bridge unreachable: {type(e).__name__}: {e}"}

    return {
        "finance": _state_to_dict(new_state),
        "bridge": bridge_response,
    }


def _proxy_bridge_halt(req: HaltRequest) -> dict[str, Any]:
    """mode=hard 用: bridge にそのままプロキシ (現状維持)。"""
    url = _bridge_url()
    try:
        resp = httpx.post(
            f"{url}/admin/halt",
            json={"mode": req.mode, "reason": req.reason},
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        logger.warning(
            f"[ADMIN] proxy halt mode={req.mode} reason={req.reason!r}"
        )
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            e.response.status_code,
            f"bridge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"bridge unreachable: {type(e).__name__}: {e}")


def _state_to_dict(s) -> dict[str, Any]:
    """HaltState → JSON-serializable dict (asdict 相当)。"""
    return {
        "soft_halted": s.soft_halted,
        "auto_triggered": s.auto_triggered,
        "reason": s.reason,
        "since": s.since,
        "triggered_by": s.triggered_by,
    }


@router.post("/resume", dependencies=[Depends(verify_api_key)])
def admin_resume() -> dict[str, Any]:
    """soft halt を解除する (finance + bridge の両方)。

    フロー:
      ① bridge /health を同期確認 (timeout=5s)。失敗時は 503 で拒否し halt 維持。
      ② finance halt.json をクリア (常に成功)。
      ③ bridge /admin/resume POST best-effort (失敗時はレスポンスに error を含める)。

    ① で gate することで、resume 直後に trading_cycle が走った際に bridge が
    まだ不通という gap を防ぐ (ユーザー意図と実態を一致させる)。

    hard halt 中は bridge /admin/resume が 403 を返し、ここでも 403 を返す。
    """
    if state.config is not None and state.config.mode == "paper":
        raise HTTPException(
            status_code=400,
            detail="resume is not available in paper mode (no bridge to resume)",
        )
    bridge_url = _bridge_url()

    # ① bridge /health を同期確認
    try:
        r = httpx.get(
            f"{bridge_url}/health",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"bridge /health unreachable: {type(e).__name__}: {e}. "
                "fix bridge first then retry."
            ),
        )

    # ② finance halt.json クリア
    from src.persistence import halt_state
    assert state.config is not None
    new_state = halt_state.clear(state.config.state_dir)
    logger.warning("[ADMIN] manual resume — finance halt cleared")

    # ③ bridge /admin/resume POST best-effort
    bridge_response: dict[str, Any]
    try:
        resp = httpx.post(
            f"{bridge_url}/admin/resume",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        bridge_response = resp.json()
    except httpx.HTTPStatusError as e:
        # bridge が 403 を返したら finance も 403 (hard halt 中の resume 拒否)
        # finance state はもう clear 済みだが、bridge との整合性を取るため halt
        # を再設定する (rare path)
        if e.response.status_code == 403:
            halt_state.trigger_manual(
                state.config.state_dir,
                reason="bridge rejected resume (hard halt)",
            )
            raise HTTPException(403, e.response.text)
        raise HTTPException(
            e.response.status_code,
            f"bridge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.HTTPError as e:
        logger.warning(
            f"[ADMIN] bridge resume POST failed "
            f"(finance halt already cleared): {e}"
        )
        bridge_response = {"error": f"bridge: {type(e).__name__}: {e}"}

    return {
        "finance": _state_to_dict(new_state),
        "bridge": bridge_response,
    }
