"""市場開閉状態の遷移トラッカー。

閉場中のジョブスキップを静かに行いつつ、状態遷移 (open↔closed) と
閉場継続中の定期ハートビートだけをログに残す。

用途:
- JobGuard の ``skip_predicate`` として渡し、閉場中は spawn 自体を抑制する
- LLM スロット経由のジョブからは ``market_skip_check()`` を直接呼び出す
- 起動時に1回呼び出しておけば、休場中起動なら初期状態をログ表示する
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from src.trading.market_hours import is_market_open, market_status_label

logger = logging.getLogger(__name__)

_DEFAULT_HEARTBEAT_INTERVAL = timedelta(hours=6)


def _to_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


class MarketStateTracker:
    """市場状態の遷移を追跡しログを制御する (プロセス共有シングルトン想定)。

    ``should_skip()`` はジョブ側から毎サイクル呼ばれる前提で、
      - 初回呼び出し時 (閉場中なら初期状態をログ)
      - 閉場 ↔ 開場 の遷移時
      - 閉場継続中 ``heartbeat_interval`` 経過ごと
    のいずれかのタイミングだけ INFO ログを発行する。
    それ以外の呼び出しは完全に無音。
    """

    def __init__(
        self,
        heartbeat_interval: timedelta = _DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._lock = threading.Lock()
        self._heartbeat_interval = heartbeat_interval
        self._last_known_open: bool | None = None
        self._last_heartbeat_at: datetime | None = None

    def should_skip(self, now: datetime | None = None) -> bool:
        """閉場中なら True (= スキップすべき)。副作用で遷移/ハートビートログを出す。"""
        open_now = is_market_open(now)
        wall_clock = _to_utc(now)

        with self._lock:
            prev = self._last_known_open

            if prev is None:
                # 初回呼び出し: 休場中に起動した場合のみ、状態をログ表示する
                self._last_known_open = open_now
                if not open_now:
                    logger.info(
                        f"Market {market_status_label(now)}. "
                        f"Scheduled jobs paused until market open."
                    )
                    self._last_heartbeat_at = wall_clock
            elif prev != open_now:
                # 状態遷移 — 1回だけログ
                self._last_known_open = open_now
                if open_now:
                    logger.info("Market opened. Resuming scheduled jobs.")
                    self._last_heartbeat_at = None
                else:
                    logger.info(
                        f"Market {market_status_label(now)}. "
                        f"Scheduled jobs paused until market open."
                    )
                    self._last_heartbeat_at = wall_clock
            elif not open_now:
                # 閉場継続中 — ハートビートチェック
                last = self._last_heartbeat_at
                if last is None or (wall_clock - last) >= self._heartbeat_interval:
                    logger.info(
                        f"Market still {market_status_label(now)}. "
                        f"Scheduler alive, jobs paused."
                    )
                    self._last_heartbeat_at = wall_clock

            return not open_now

    def reset(self) -> None:
        """テスト用: 状態を初期化する。"""
        with self._lock:
            self._last_known_open = None
            self._last_heartbeat_at = None


_tracker = MarketStateTracker()


def market_skip_check(now: datetime | None = None) -> bool:
    """閉場中なら True を返し、必要なら遷移/ハートビートログを発行する。"""
    return _tracker.should_skip(now)


def reset_market_tracker() -> None:
    """テスト用: モジュール共有トラッカーを初期化する。"""
    _tracker.reset()
