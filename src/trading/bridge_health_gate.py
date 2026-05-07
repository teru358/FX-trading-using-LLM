"""bridge プリフライト + halt 連携の単一ゲート。

各 cycle (tech / 取引 / price_monitor) は本処理の前に probe() を呼ぶ。
1 回目失敗時は 1 分待機して同一 cycle 内でリトライ、リトライも失敗で halt 発動。
balance 同期も probe() 内で行う (定期 heartbeat 廃止)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    ok: bool
    mt5_connected: bool
    latency_ms: float | None
    http_status: int | None
    error: str | None
    retried: bool


class BridgeHealthGate:
    """bridge プリフライト + halt 連携の単一ゲート。"""

    def __init__(
        self, *,
        config,
        notifier=None,
        log_path: Path | None = None,
        retry_after_sec: float = 60.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._log_path = log_path
        self._retry_after_sec = retry_after_sec
        self._sleep = sleep_fn
        self._enabled = (
            getattr(config, "mode", None) in ("live", "live_test")
            and getattr(config, "live_broker", None) in ("mt5", "oanda")
            and (
                getattr(config.providers, "mt5", None) is not None
                or getattr(config.providers, "oanda", None) is not None
            )
        )

    def probe(self, *, caller: str, sync_balance: bool) -> ProbeResult:
        """bridge /health プローブ + 1分後リトライ。リトライも失敗で halt 発動。"""
        if not self._enabled:
            return ProbeResult(
                ok=True, mt5_connected=True, latency_ms=None,
                http_status=None, error=None, retried=False,
            )
        first = self._do_probe()
        self._append_log(caller, first, retry=False)
        if first.ok:
            if sync_balance and self._config.mode == "live":
                self._sync_balance()
            return first

        # 1 回目失敗 → 1 分待ってリトライ
        logger.warning(
            f"[BRIDGE_GATE] {caller}: /health failed "
            f"({first.error or first.http_status}), retrying in "
            f"{self._retry_after_sec:.0f}s"
        )
        self._sleep(self._retry_after_sec)
        second = self._do_probe()
        second_retried = ProbeResult(**{**asdict(second), "retried": True})
        self._append_log(caller, second_retried, retry=True)

        if second.ok:
            if sync_balance and self._config.mode == "live":
                self._sync_balance()
            return second_retried

        # リトライも失敗 → halt 発動
        self._trigger_halt(
            reason=(
                f"{caller}: /health failed twice "
                f"(1st={first.error or first.http_status}, "
                f"2nd={second.error or second.http_status})"
            ),
        )
        return second_retried

    def _do_probe(self) -> ProbeResult:
        """bridge /health を 1 回叩く。OANDA は将来用 placeholder。"""
        live_broker = getattr(self._config, "live_broker", None)
        if live_broker == "oanda":
            logger.warning("[BRIDGE_GATE] oanda probe is placeholder, returning ok=True")
            return ProbeResult(
                ok=True, mt5_connected=True, latency_ms=None,
                http_status=None, error=None, retried=False,
            )
        mt5_cfg = self._config.providers.mt5
        url = f"{mt5_cfg.bridge_url.rstrip('/')}/health"
        started = time.perf_counter()
        try:
            resp = httpx.get(url, timeout=mt5_cfg.request_timeout_seconds)
            latency_ms = (time.perf_counter() - started) * 1000
            ok = resp.is_success
            mt5_connected = bool(resp.json().get("mt5_connected", False)) if ok else False
            return ProbeResult(
                ok=ok, mt5_connected=mt5_connected,
                latency_ms=round(latency_ms, 1),
                http_status=resp.status_code,
                error=None if ok else f"http_{resp.status_code}",
                retried=False,
            )
        except (httpx.HTTPError, ValueError) as e:
            latency_ms = (time.perf_counter() - started) * 1000
            return ProbeResult(
                ok=False, mt5_connected=False,
                latency_ms=round(latency_ms, 1),
                http_status=None,
                error=f"{type(e).__name__}: {e}",
                retried=False,
            )

    def _append_log(self, caller: str, result: ProbeResult, *, retry: bool) -> None:
        if self._log_path is None:
            return
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "caller": caller,
            "ok": result.ok,
            "mt5_connected": result.mt5_connected,
            "latency_ms": result.latency_ms,
            "http_status": result.http_status,
            "error": result.error,
            "retried": retry,
        }
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _sync_balance(self) -> None:
        """bridge /account を叩いて balance.json を更新する。

        失敗時は警告ログのみ (halt は発動しない、health は成功しているため)。
        """
        mt5_cfg = self._config.providers.mt5
        if mt5_cfg is None:
            return
        headers = {"X-Bridge-Api-Key": mt5_cfg.api_key} if mt5_cfg.api_key else {}
        try:
            resp = httpx.get(
                f"{mt5_cfg.bridge_url.rstrip('/')}/account",
                timeout=mt5_cfg.request_timeout_seconds, headers=headers,
            )
            resp.raise_for_status()
            mt5_balance = float(resp.json()["balance"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(
                f"[BRIDGE_GATE] /account fetch failed: {type(e).__name__}: {e}"
            )
            return

        from src.persistence import balance_snapshot

        captured: dict = {}

        def _swap(snap):
            captured["prev_source"] = snap.source
            return balance_snapshot.refresh_from_mt5(snap, mt5_balance)

        new_snap = balance_snapshot.mutate(self._config.state_dir, _swap)
        prev_source = captured.get("prev_source")
        if prev_source == "paper" and new_snap.source == "mt5":
            logger.info(
                f"[BRIDGE_GATE] balance.json source paper → mt5: "
                f"deposit ¥{mt5_balance:,.0f} (initial)"
            )

    def _trigger_halt(self, reason: str) -> None:
        """halt_state.trigger_auto + Discord 通知 + bridge POST best-effort。

        bridge POST は best-effort (失敗しても finance halt は確定済)。
        Discord 通知は changed=False で多重防止。
        """
        from src.persistence import halt_state

        state_dir = self._config.state_dir
        _new, changed = halt_state.trigger_auto(
            state_dir, reason, "bridge_health_gate",
        )
        if not changed:
            logger.debug("[BRIDGE_GATE] halt skipped: already halted")
            return

        logger.error(f"[BRIDGE_GATE] AUTO SOFT HALT triggered: {reason}")

        # Discord 通知
        if self._notifier is not None:
            try:
                coro = self._notifier.send_embed(
                    title="🛑 MT5 ブリッジ /health 不通 → 自動 SOFT HALT",
                    description=(
                        f"reason: {reason}\n\n"
                        f"bridge `/health` がリトライ含め 2 回連続不通。\n"
                        f"新規発注をブロックしました (既存ポジ管理は継続)。\n\n"
                        f"復帰: bridge 復活確認後 `?resume`"
                    ),
                    color=0xE74C3C,
                )
                if asyncio.iscoroutine(coro):
                    asyncio.run(coro)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[BRIDGE_GATE] notify halt failed: {e}")

        # bridge /admin/halt POST best-effort
        mt5_cfg = self._config.providers.mt5
        if mt5_cfg is None or not mt5_cfg.bridge_url:
            return
        headers = {"X-Bridge-Api-Key": mt5_cfg.api_key} if mt5_cfg.api_key else {}
        try:
            httpx.post(
                f"{mt5_cfg.bridge_url.rstrip('/')}/admin/halt",
                json={"mode": "soft", "reason": f"auto: {reason}"},
                timeout=5.0, headers=headers,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[BRIDGE_GATE] bridge halt POST failed "
                f"(state already set locally): {e}"
            )
