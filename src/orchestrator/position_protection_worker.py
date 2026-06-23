"""tick 駆動のポジション保護 worker (spec §5)。

apply_protection helper (Task 7.5) を price_monitor と共有し、副作用一式を同一化する
(review H-b)。close は実行しない (spec §5.1.1, H4)。
- protect_shadow: execute=False で判定を protection_decisions に記録のみ。
- protect_live: execute=True で helper の副作用一式 (remote-first SL 適用 + state 更新) を実行。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from src.trading.protection_apply import apply_protection
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


class PriceProtectionWorker:
    def __init__(
        self, *, producer, position_provider: Callable[[], list],
        store, cfg, position_mgr, broker, mode: str,
        remote_sync_enabled: bool = True, poll_seconds: int = 2,
    ) -> None:
        if mode not in ("protect_shadow", "protect_live"):
            raise ValueError(
                f"PriceProtectionWorker.mode must be 'protect_shadow' or "
                f"'protect_live', got {mode!r} (the worker is only built for "
                f"protect stages; other modes would silently no-op)"
            )
        self._producer = producer
        self._positions = position_provider
        self._store = store
        self._cfg = cfg
        self._position_mgr = position_mgr
        self._broker = broker
        self._mode = mode  # protect_shadow | protect_live
        self._remote_sync_enabled = remote_sync_enabled
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def run_once(self) -> None:
        if self._mode not in ("protect_shadow", "protect_live"):
            return
        execute = self._mode == "protect_live"
        for pos in self._positions():
            snap = self._producer.latest(pos.pair)
            if snap is None:
                continue
            try:
                res = apply_protection(
                    pos, current=snap.mid, cfg=self._cfg,
                    position_mgr=self._position_mgr, broker=self._broker,
                    remote_sync_enabled=self._remote_sync_enabled, execute=execute,
                )
            except Exception:
                logger.exception("[PROT-WORKER] apply failed for %s", pos.order_id)
                continue

            self._store.record_protection_decision(
                ts=db_now(), pair=pos.pair, order_id=pos.order_id,
                source="tick_worker", action=res.action, stage=res.stage,
                target_sl=res.target_sl, mfe_r=res.mfe_r, giveback_r=res.giveback_r,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("[PROT-WORKER] run_once failed")
            self._stop.wait(timeout=self._poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="prot-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
