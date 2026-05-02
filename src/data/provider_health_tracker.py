"""5分窓 2 回失敗判定 + degraded 状態管理。

OHLCV プロバイダ降格判定で PriceProvider が利用する。
直近 N 回の失敗を窓で集計し、degraded 遷移・recovery 遷移時に True を返す。
通知は呼出側 (PriceProvider など) で transitioning=True の時のみ発火。

Phase 4 で発注経路の判定 (Mt5BridgeBrokerAdapter._consecutive_rejects) と
統合検討の余地あり。
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta


class ProviderHealthTracker:
    def __init__(
        self, *, failure_window_sec: int = 300, failure_threshold: int = 2,
    ) -> None:
        self._window = timedelta(seconds=failure_window_sec)
        self._threshold = failure_threshold
        self._failures: deque[datetime] = deque(maxlen=failure_threshold)
        self._degraded = False
        self._lock = threading.Lock()

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def record_failure(self, now: datetime) -> bool:
        """失敗を記録。degraded に遷移したら True を返す。"""
        with self._lock:
            self._failures.append(now)
            if len(self._failures) < self._threshold:
                return False
            oldest = self._failures[0]
            within_window = (now - oldest) <= self._window
            if within_window and not self._degraded:
                self._degraded = True
                return True
            return False

    def record_success(self, now: datetime) -> bool:
        """成功を記録。recovery (degraded → 通常) したら True を返す。"""
        with self._lock:
            self._failures.clear()
            if self._degraded:
                self._degraded = False
                return True
            return False
