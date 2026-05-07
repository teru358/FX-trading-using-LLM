"""bridge プリフライト + halt 連携の単一ゲート。

各 cycle (tech / 取引 / price_monitor) は本処理の前に probe() を呼ぶ。
1 回目失敗時は 1 分待機して同一 cycle 内でリトライ、リトライも失敗で halt 発動。
balance 同期も probe() 内で行う (定期 heartbeat 廃止)。
"""
from __future__ import annotations

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
        # 残りの実装は Task 3 以降で追加
        raise NotImplementedError("filled in next tasks")
