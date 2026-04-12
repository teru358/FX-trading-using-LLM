"""LLM 呼び出し用サーキットブレーカー。

連続失敗が閾値を超えると一定時間 LLM 呼び出しをスキップし、
ニュートラルなフォールバック応答を返す。クールダウン経過後に自動復帰する。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """シンプルなサーキットブレーカー。

    状態:
        CLOSED  — 正常。呼び出し許可。
        OPEN    — 障害中。呼び出しをスキップ。
        HALF_OPEN — クールダウン経過。1回だけ試行を許可。

    Parameters:
        failure_threshold: OPEN に遷移する連続失敗数 (default: 3)
        cooldown_seconds: OPEN 状態を維持する秒数 (default: 300)
        name: ログ表示名
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: int = 300,
        name: str = "LLM",
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._name = name
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            return False
        return True

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "CLOSED"
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self._cooldown:
            return "HALF_OPEN"
        return "OPEN"

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            logger.info(
                f"[CB/{self._name}] Recovered after {self._consecutive_failures} failures"
            )
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning(
                f"[CB/{self._name}] Circuit OPEN — "
                f"{self._consecutive_failures} consecutive failures, "
                f"cooldown {self._cooldown}s"
            )

    def allow_request(self) -> bool:
        """呼び出しを許可するかどうかを返す。"""
        state = self.state
        if state == "CLOSED":
            return True
        if state == "HALF_OPEN":
            logger.info(f"[CB/{self._name}] HALF_OPEN — allowing probe request")
            return True
        remaining = self._cooldown - (time.monotonic() - (self._opened_at or 0))
        logger.debug(
            f"[CB/{self._name}] Circuit OPEN — skipping request ({remaining:.0f}s remaining)"
        )
        return False
