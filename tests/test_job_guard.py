"""JobGuard のユニットテスト。"""
from __future__ import annotations

import threading
import time

import pytest

from src.concurrency.job_guard import JobGuard


def test_spawn_if_idle_first_call_returns_true():
    guard = JobGuard("test")
    called = threading.Event()

    def _fn():
        called.set()

    assert guard.spawn_if_idle(_fn) is True
    assert called.wait(timeout=1.0)


def test_spawn_if_idle_blocks_duplicate_while_running():
    guard = JobGuard("test")
    started = threading.Event()
    release = threading.Event()

    def _long_fn():
        started.set()
        release.wait(timeout=5.0)

    assert guard.spawn_if_idle(_long_fn) is True
    assert started.wait(timeout=1.0)

    # 実行中なので2回目は False
    assert guard.spawn_if_idle(_long_fn) is False

    # 解放して待つ
    release.set()
    time.sleep(0.2)

    # 完了後は再度 spawn 可能
    second_started = threading.Event()
    assert guard.spawn_if_idle(lambda: second_started.set()) is True
    assert second_started.wait(timeout=1.0)


def test_skip_predicate_blocks_spawn_silently():
    """skip_predicate が True を返すと fn は呼ばれず False が返る。"""
    called = threading.Event()
    guard = JobGuard("test", skip_predicate=lambda: True)

    assert guard.spawn_if_idle(lambda: called.set()) is False
    time.sleep(0.1)
    assert not called.is_set()
    # 実行扱いにならないので is_running も False のまま
    assert guard.is_running is False


def test_skip_predicate_false_allows_spawn():
    """skip_predicate が False を返すと通常通り spawn される。"""
    called = threading.Event()
    guard = JobGuard("test", skip_predicate=lambda: False)

    assert guard.spawn_if_idle(lambda: called.set()) is True
    assert called.wait(timeout=1.0)


def test_skip_predicate_toggles_between_calls():
    """skip_predicate が呼び出しごとに True/False を切り替えても正しく動く。"""
    allowed = [False]
    call_count = [0]

    def _predicate() -> bool:
        return not allowed[0]

    def _fn() -> None:
        call_count[0] += 1

    guard = JobGuard("test", skip_predicate=_predicate)

    # 最初はブロック
    assert guard.spawn_if_idle(_fn) is False
    time.sleep(0.05)
    assert call_count[0] == 0

    # 許可してから呼び直す
    allowed[0] = True
    assert guard.spawn_if_idle(_fn) is True
    time.sleep(0.2)
    assert call_count[0] == 1


def test_is_running_reflects_state():
    guard = JobGuard("test")
    assert guard.is_running is False

    release = threading.Event()
    started = threading.Event()

    def _fn():
        started.set()
        release.wait(timeout=5.0)

    guard.spawn_if_idle(_fn)
    started.wait(timeout=1.0)
    assert guard.is_running is True

    release.set()
    time.sleep(0.2)
    assert guard.is_running is False


def test_last_elapsed_sec_recorded_after_run():
    guard = JobGuard("test")
    done = threading.Event()

    def _fn():
        time.sleep(0.1)
        done.set()

    guard.spawn_if_idle(_fn)
    done.wait(timeout=1.0)
    time.sleep(0.1)  # 後処理を待つ

    assert guard.last_elapsed_sec is not None
    assert guard.last_elapsed_sec >= 0.1


def test_exception_in_fn_releases_guard():
    guard = JobGuard("test")

    def _bad_fn():
        raise ValueError("boom")

    guard.spawn_if_idle(_bad_fn)
    time.sleep(0.2)

    # 例外で死んでも guard は解放されている
    assert guard.is_running is False

    second_started = threading.Event()
    assert guard.spawn_if_idle(lambda: second_started.set()) is True
    assert second_started.wait(timeout=1.0)
