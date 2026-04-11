"""JobGuard: 個別ジョブの重複実行防止用ガード。

price_monitor のような軽量ジョブが重複起動されるのを防ぐ。
LLM を使うジョブは PriorityJobSlot を使うこと。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


class JobGuard:
    """ジョブ1種類分の実行中フラグと重複防止。

    同じガードインスタンスを共有するジョブは、実行中なら2回目は拒否される。
    異なるガードインスタンスのジョブは完全独立 (並行可)。
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None
        self._last_elapsed_sec: float | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def started_at(self) -> datetime | None:
        with self._lock:
            return self._started_at

    @property
    def last_elapsed_sec(self) -> float | None:
        with self._lock:
            return self._last_elapsed_sec

    def spawn_if_idle(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """実行中でなければ別スレッドで fn を spawn する。

        既に実行中なら False を返し、warning ログを出す。
        fn が例外を投げても guard は確実に解放される。
        """
        with self._lock:
            if self._running:
                elapsed = (datetime.now() - self._started_at).total_seconds() if self._started_at else 0
                logger.warning(
                    f"[JOB] {self._name} skipped: previous run still active ({elapsed:.0f}s)"
                )
                return False
            self._running = True
            self._started_at = datetime.now()

        def _worker() -> None:
            start = time.monotonic()
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error(f"[JOB] {self._name} failed: {e}", exc_info=True)
            finally:
                elapsed_sec = time.monotonic() - start
                with self._lock:
                    self._running = False
                    self._started_at = None
                    self._last_elapsed_sec = elapsed_sec
                logger.info(f"[JOB] {self._name} completed in {elapsed_sec:.1f}s")

        thread = threading.Thread(
            target=_worker,
            name=f"job-{self._name}",
            daemon=True,
        )
        thread.start()
        return True
