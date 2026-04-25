"""LLM 呼び出し用サーキットブレーカー。

連続失敗が閾値を超えると一定時間 LLM 呼び出しをスキップし、
ニュートラルなフォールバック応答を返す。クールダウン経過後に自動復帰する。

エラー履歴・usage_limit ヒット数も保持し、/usage endpoint で expose する。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorEvent:
    """エラー発生イベント 1 件分。"""
    at: float            # UNIX time (time.time())
    error_type: str      # 例: "ClaudeCliUsageLimitError"
    message: str         # 先頭 200 文字に切り詰め


class CircuitBreaker:
    """シンプルなサーキットブレーカー + エラー計測。

    状態:
        CLOSED  — 正常。呼び出し許可。
        OPEN    — 障害中。呼び出しをスキップ。
        HALF_OPEN — クールダウン経過。1回だけ試行を許可。

    Parameters:
        failure_threshold: OPEN に遷移する連続失敗数 (default: 3)
        cooldown_seconds: OPEN 状態を維持する秒数 (default: 300)
        name: ログ表示名
        history_size: エラー履歴リングバッファの長さ (default: 10)
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: int = 300,
        name: str = "LLM",
        history_size: int = 10,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._name = name
        self._consecutive_failures = 0
        self._opened_at: float | None = None  # monotonic 時間 (状態判定用)

        # 計測用カウンター
        self._total_success = 0
        self._total_failure = 0
        self._usage_limit_hits = 0
        # 直近エラー履歴 (古いものから自動破棄)
        self._error_history: deque[ErrorEvent] = deque(maxlen=history_size)

    # ── 状態プロパティ ──────────────────────────────────────────────

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

    @property
    def cooldown_remaining_seconds(self) -> float:
        """OPEN 状態のとき、残りクールダウン秒数を返す。それ以外は 0。"""
        if self._opened_at is None:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        remaining = self._cooldown - elapsed
        return max(0.0, remaining)

    # ── 記録 API ───────────────────────────────────────────────────

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            logger.info(
                f"[CB/{self._name}] Recovered after {self._consecutive_failures} failures"
            )
        self._consecutive_failures = 0
        self._opened_at = None
        self._total_success += 1

    def record_failure(
        self,
        error_type: str = "",
        message: str = "",
        is_usage_limit: bool = False,
    ) -> None:
        """失敗を記録する。

        Args:
            error_type: 例外クラス名 (例: "ClaudeCliUsageLimitError")
            message: エラーメッセージ (長すぎる場合は 200 文字に切り詰める)
            is_usage_limit: usage_limit 系エラーかどうか (別カウンターで集計)
        """
        self._consecutive_failures += 1
        self._total_failure += 1
        if is_usage_limit:
            self._usage_limit_hits += 1

        self._error_history.append(ErrorEvent(
            at=time.time(),
            error_type=error_type or "Unknown",
            message=(message or "")[:200],
        ))

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
        remaining = self.cooldown_remaining_seconds
        logger.debug(
            f"[CB/{self._name}] Circuit OPEN — skipping request ({remaining:.0f}s remaining)"
        )
        return False

    # ── 集計用スナップショット ──────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """/usage endpoint 等で JSON 化する現在状態。"""
        history = list(self._error_history)
        last = history[-1] if history else None
        return {
            "name":                  self._name,
            "state":                 self.state,
            "consecutive_failures":  self._consecutive_failures,
            "cooldown_remaining_s":  round(self.cooldown_remaining_seconds, 1),
            "total_success":         self._total_success,
            "total_failure":         self._total_failure,
            "usage_limit_hits":      self._usage_limit_hits,
            "last_error": {
                "type":    last.error_type,
                "message": last.message,
                "at":      last.at,
            } if last else None,
            "recent_errors": [
                {"type": e.error_type, "at": e.at, "message": e.message}
                for e in history
            ],
        }
