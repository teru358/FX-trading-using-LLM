"""MT5 ブリッジ稼働率測定 + auto soft halt ジョブ。

Windows 側で動く MT5 ブリッジ (FastAPI) の `/health` を定期的に叩き、
応答有無・レイテンシを JSONL に追記する。さらに `/health` が
連続 N 回不通になった場合は bridge `/admin/halt mode=soft` を発動し、
Discord に通知する。

設計:
- bridge_url が空ならノーオペ (まだブリッジが立っていない初期段階で安全)
- 失敗 (接続不可・タイムアウト・5xx) も等しく 1 行記録 → 稼働率分母に含める
- 失敗してもデーモンは止めない (logger.warning のみ)
- 連続 `consecutive_unreachable_threshold` 回失敗で `/admin/halt mode=soft` 1 回発動
  (再開は手動 `/admin/resume`)。デフォルト heartbeat=5 分 × 3 = 約 15 分で halt。
- 発注経路 (Mt5BridgeBrokerAdapter) の auto soft halt とは独立カウンタ。
  どちらが先に閾値に達してもよく、bridge 側で多重 halt は冪等。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from src.config import BASE_DIR, AppConfig
from src.utils.clock import local_now

logger = logging.getLogger(__name__)


# ── モジュールレベル状態 (単一プロセス前提、再起動でリセット) ──
_state: dict = {
    "consecutive_failures": 0,
    "auto_halt_triggered": False,
}


def _reset_state_for_test() -> None:
    """ユニットテスト用: モジュール状態をリセット。"""
    _state["consecutive_failures"] = 0
    _state["auto_halt_triggered"] = False


def _append_record(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _probe(url: str, timeout: float) -> tuple[bool, float | None, int | None, str | None]:
    """ブリッジ /health に GET。Returns: (ok, latency_ms, http_status, error)."""
    started = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.perf_counter() - started) * 1000
        return (resp.is_success, latency_ms, resp.status_code, None)
    except httpx.HTTPError as e:
        latency_ms = (time.perf_counter() - started) * 1000
        return (False, latency_ms, None, f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000
        return (False, latency_ms, None, f"{type(e).__name__}: {e}")


def _trigger_auto_soft_halt(
    config: AppConfig, base_url: str, api_key: str, timeout: float,
    failure_count: int,
) -> None:
    """bridge /admin/halt mode=soft を発動し、Discord 通知を送る。

    halt API 呼出失敗時は通知も諦める (logger.error のみ)。
    notifier が無効でも asyncio.run(NullNotifier.send_embed(...)) は安全。
    """
    headers = {"X-Bridge-Api-Key": api_key} if api_key else {}
    reason = f"{failure_count} consecutive /health failures"
    try:
        httpx.post(
            f"{base_url}/admin/halt",
            json={"mode": "soft", "reason": f"auto: {reason}"},
            timeout=timeout, headers=headers,
        )
        logger.error(f"[MT5_HB] AUTO SOFT HALT triggered: {reason}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[MT5_HB] auto halt API call failed: {e}")
        return

    if not config.notifier.enabled:
        return

    try:
        from src.notifications.notifier import create_notifier
        notifier = create_notifier(config.notifier.enabled)
        asyncio.run(notifier.send_embed(
            title="🛑 MT5 ブリッジ /health 不通 → 自動 SOFT HALT",
            description=(
                f"reason: {reason}\n\n"
                f"bridge `/health` が {failure_count} 回連続応答なし。\n"
                f"新規発注をブロックしました (既存ポジ管理は継続)。\n\n"
                f"復帰: bridge 復活確認後 `?resume`"
            ),
            color=0xE74C3C,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[MT5_HB] auto halt notify failed: {e}")


def run_mt5_heartbeat(config: AppConfig) -> None:
    """schedule ライブラリから呼ばれる同期エントリポイント。

    live_broker != "mt5" もしくは providers.mt5 未設定、または
    bridge_url 空ならノーオペで終了。
    失敗してもデーモンは止まらない。
    """
    if config.live_broker != "mt5" or config.providers.mt5 is None:
        logger.debug("[MT5_HB] live_broker != mt5, skipping")
        return
    cfg = config.providers.mt5
    base = cfg.bridge_url.rstrip("/")
    if not base:
        logger.debug("[MT5_HB] bridge_url not configured, skipping")
        return

    url = f"{base}/health"
    try:
        ok, latency_ms, status, error = _probe(url, cfg.request_timeout_seconds)
        record = {
            "ts": local_now(config).isoformat(timespec="seconds"),
            "url": url,
            "ok": ok,
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "http_status": status,
            "error": error,
        }
        log_path = BASE_DIR / cfg.log_path
        _append_record(log_path, record)

        if ok:
            _state["consecutive_failures"] = 0
            _state["auto_halt_triggered"] = False
            logger.info(
                f"[MT5_HB] OK  status={status} latency={record['latency_ms']}ms"
            )
            return

        _state["consecutive_failures"] += 1
        logger.warning(
            f"[MT5_HB] FAIL ({_state['consecutive_failures']}/"
            f"{cfg.consecutive_unreachable_threshold}) "
            f"status={status} latency={record['latency_ms']}ms err={error}"
        )

        # ── auto soft halt 判定 ──
        if _state["auto_halt_triggered"]:
            return  # 既発動 → 復帰 (=success) するまで再発動しない
        if _state["consecutive_failures"] >= cfg.consecutive_unreachable_threshold:
            _trigger_auto_soft_halt(
                config, base, cfg.api_key,
                cfg.request_timeout_seconds,
                _state["consecutive_failures"],
            )
            _state["auto_halt_triggered"] = True
    except Exception as e:  # noqa: BLE001 - デーモン停止防止
        logger.error(f"[MT5_HB] heartbeat job failed: {e}", exc_info=True)
