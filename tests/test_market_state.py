"""MarketStateTracker の遷移/ハートビートログを検証する。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.trading.market_state import MarketStateTracker


@pytest.fixture
def tracker() -> MarketStateTracker:
    return MarketStateTracker(heartbeat_interval=timedelta(hours=6))


def _paused_lines(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "paused until market open" in r.message]


def _still_lines(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "Scheduler alive" in r.message]


def _resumed_lines(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "Resuming" in r.message]


def test_initial_call_when_closed_logs_paused(tracker, caplog):
    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            assert tracker.should_skip() is True

    assert len(_paused_lines(caplog.records)) == 1


def test_initial_call_when_open_is_silent(tracker, caplog):
    with patch("src.trading.market_state.is_market_open", return_value=True):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            assert tracker.should_skip() is False

    assert caplog.records == []


def test_repeated_closed_calls_do_not_spam(tracker, caplog):
    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            for _ in range(20):
                assert tracker.should_skip() is True

    # 初期状態ログ1回のみ (ハートビートは6h 経たないので出ない)
    assert len(_paused_lines(caplog.records)) == 1
    assert _still_lines(caplog.records) == []


def test_transition_closed_to_open_logs_resume(tracker, caplog):
    with patch("src.trading.market_state.is_market_open", return_value=False):
        tracker.should_skip()
    caplog.clear()
    with patch("src.trading.market_state.is_market_open", return_value=True):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            assert tracker.should_skip() is False
            # 2回目以降は無音
            assert tracker.should_skip() is False
            assert tracker.should_skip() is False

    assert len(_resumed_lines(caplog.records)) == 1


def test_transition_open_to_closed_logs_paused(tracker, caplog):
    with patch("src.trading.market_state.is_market_open", return_value=True):
        tracker.should_skip()
    caplog.clear()
    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            assert tracker.should_skip() is True
            # 2回目以降は無音
            assert tracker.should_skip() is True

    assert len(_paused_lines(caplog.records)) == 1


def test_heartbeat_fires_after_interval(tracker, caplog):
    t0 = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)       # Saturday UTC → closed
    t5h = t0 + timedelta(hours=5)
    t6h_plus = t0 + timedelta(hours=6, seconds=1)
    t12h_plus = t0 + timedelta(hours=12, seconds=2)

    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            tracker.should_skip(now=t0)       # 初期 paused ログ
            tracker.should_skip(now=t5h)      # 6h 未満 → 無音
            tracker.should_skip(now=t6h_plus)  # 6h 超 → heartbeat
            tracker.should_skip(now=t12h_plus) # さらに 6h 超 → heartbeat

    assert len(_paused_lines(caplog.records)) == 1
    assert len(_still_lines(caplog.records)) == 2


def test_heartbeat_resets_after_reopen_and_reclose(tracker, caplog):
    t0 = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)

    # 1. 閉場 → 初期ログ
    with patch("src.trading.market_state.is_market_open", return_value=False):
        tracker.should_skip(now=t0)

    # 2. 開場 → resume ログ
    with patch("src.trading.market_state.is_market_open", return_value=True):
        tracker.should_skip(now=t0 + timedelta(hours=1))

    # 3. 再閉場 → 新しい paused ログ + ハートビート初期化
    caplog.clear()
    t_reclose = t0 + timedelta(hours=2)
    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            tracker.should_skip(now=t_reclose)
            # すぐの呼び出しは heartbeat 出さない (6h 経ってない)
            tracker.should_skip(now=t_reclose + timedelta(hours=5))

    assert len(_paused_lines(caplog.records)) == 1
    assert _still_lines(caplog.records) == []


def test_naive_datetime_accepted(tracker, caplog):
    # tzinfo なしの datetime を渡してもクラッシュしないこと
    t = datetime(2026, 4, 11, 10, 0)  # naive
    with patch("src.trading.market_state.is_market_open", return_value=False):
        with caplog.at_level(logging.INFO, logger="src.trading.market_state"):
            assert tracker.should_skip(now=t) is True
            assert tracker.should_skip(now=t + timedelta(hours=7)) is True

    assert len(_paused_lines(caplog.records)) == 1
    assert len(_still_lines(caplog.records)) == 1
