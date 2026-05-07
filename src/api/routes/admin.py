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
    """mode=hard 用: bridge にプロキシ。

    bridge POST が失敗した場合は finance 側 soft halt を立てて新規発注を止める
    fail-safe (hard halt 未達でも最低限の防御)。レスポンスは 502 で finance state
    を含めて返す。
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
        # bridge 不通 = hard halt 未達 → finance soft halt で発注を止める
        from src.persistence import halt_state
        assert state.config is not None
        new_state, _changed = halt_state.trigger_manual(
            state.config.state_dir,
            reason=f"manual hard halt delivery failed: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[ADMIN] hard halt POST failed; finance soft halt set: {e}"
        )
        raise HTTPException(
            502,
            {
                "message": "bridge hard halt failed; finance soft halt is active",
                "finance": _state_to_dict(new_state),
                "error": f"{type(e).__name__}: {e}",
            },
        )


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
    """soft halt を解除する (finance + bridge の両方の受付状態確認)。

    フロー:
      ① bridge /health を同期確認 (mt5_connected=true まで含めて)
      ② bridge /admin/resume POST (must succeed)
      ③ bridge /admin/status で受付状態確認
         - live: accepts_new_orders=true 必須
         - live_test: soft_halted=false + mt5_connected=true 必須
      ④ finance halt.json クリア (上記すべて成功時のみ)

    途中で失敗した場合 finance halt は維持する。
    hard halt 中 (bridge resume 403) は finance halt も維持。
    """
    if state.config is not None and state.config.mode == "paper":
        raise HTTPException(
            status_code=400,
            detail="resume is not available in paper mode (no bridge to resume)",
        )
    bridge_url = _bridge_url()
    assert state.config is not None

    # ① bridge /health 同期確認
    try:
        r = httpx.get(
            f"{bridge_url}/health",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        r.raise_for_status()
        health_data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"bridge /health unreachable: {type(e).__name__}: {e}. "
                "fix bridge first then retry."
            ),
        )

    if not health_data.get("mt5_connected"):
        raise HTTPException(
            status_code=503,
            detail=(
                "bridge is reachable but MT5 is not connected. "
                "fix MT5 first then retry."
            ),
        )

    # ② bridge /admin/resume POST (must succeed)
    try:
        resp = httpx.post(
            f"{bridge_url}/admin/resume",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        resp.raise_for_status()
        bridge_response = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            # hard halt 中 → finance halt は維持 (clear しない)
            raise HTTPException(403, e.response.text)
        raise HTTPException(
            e.response.status_code,
            f"bridge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(
            503, f"bridge resume POST failed: {type(e).__name__}: {e}",
        )

    # ③ bridge /admin/status で受付状態確認
    try:
        sresp = httpx.get(
            f"{bridge_url}/admin/status",
            timeout=5.0,
            headers=_bridge_headers(),
        )
        sresp.raise_for_status()
        status_data = sresp.json()
    except httpx.HTTPError as e:
        raise HTTPException(
            503, f"bridge status check failed: {type(e).__name__}: {e}",
        )

    if state.config.mode == "live":
        ready = bool(status_data.get("accepts_new_orders"))
    else:
        # live_test は DRY_RUN=true で accepts_new_orders=false になり得る。
        # soft_halted が解除され MT5 接続があることを確認する。
        ready = (
            not status_data.get("soft_halted")
            and bool(status_data.get("mt5_connected"))
        )

    if not ready:
        raise HTTPException(
            503,
            {
                "message": "bridge resumed but not ready to accept the configured mode",
                "bridge_status": status_data,
            },
        )

    # ④ finance halt.json クリア
    from src.persistence import halt_state
    new_state = halt_state.clear(state.config.state_dir)
    logger.warning(
        f"[ADMIN] manual resume — bridge accepted "
        f"(mode={state.config.mode}, ready=True), finance halt cleared"
    )

    return {
        "finance":       _state_to_dict(new_state),
        "bridge":        bridge_response,
        "bridge_status": status_data,
    }
