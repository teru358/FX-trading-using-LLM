"""MT5 ブリッジ稼働率測定ジョブ。

Windows 側で動く MT5 ブリッジ (FastAPI) の `/health` を定期的に叩き、
応答有無・レイテンシを JSONL に追記する。main PC が常時稼働でないため、
将来 MT5 経由で発注を始める前に「実運用でどの程度繋がっているか」を
empirical に把握する目的。

設計:
- bridge_url が空ならノーオペ (まだブリッジが立っていない初期段階で安全)
- 失敗 (接続不可・タイムアウト・5xx) も等しく 1 行記録 → 稼働率分母に含める
- 失敗してもデーモンは止めない (logger.warning のみ)
- このジョブ自体に副作用はない (照会のみ)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from src.config import BASE_DIR, AppConfig
from src.utils.clock import local_now

logger = logging.getLogger(__name__)


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
            logger.info(
                f"[MT5_HB] OK  status={status} latency={record['latency_ms']}ms"
            )
        else:
            logger.warning(
                f"[MT5_HB] FAIL status={status} latency={record['latency_ms']}ms "
                f"err={error}"
            )
    except Exception as e:  # noqa: BLE001 - デーモン停止防止
        logger.error(f"[MT5_HB] heartbeat job failed: {e}", exc_info=True)
