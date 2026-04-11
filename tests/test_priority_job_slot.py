"""PriorityJobSlot のユニットテスト。"""
from __future__ import annotations

import threading
import time

import pytest

from src.concurrency.priority_job_slot import PriorityJobSlot


# ── try_run_scheduled ─────────────────────────────────────────


def test_try_run_scheduled_empty_slot_runs():
    slot = PriorityJobSlot("test")
    called = threading.Event()

    def _fn():
        called.set()

    assert slot.try_run_scheduled(_fn) is True
    assert called.is_set()


def test_try_run_scheduled_skips_when_busy():
    slot = PriorityJobSlot("test")
    release = threading.Event()
    started = threading.Event()

    def _slow_fn():
        started.set()
        release.wait(timeout=5.0)

    # バックグラウンドで長いジョブを開始
    def _bg():
        slot.try_run_scheduled(_slow_fn)
    threading.Thread(target=_bg, daemon=True).start()
    started.wait(timeout=1.0)

    # この間にスキップされる
    skipped_fn_called = threading.Event()
    assert slot.try_run_scheduled(lambda: skipped_fn_called.set()) is False
    assert not skipped_fn_called.is_set()

    release.set()
    time.sleep(0.2)


# ── try_run_user_sync ─────────────────────────────────────────


def test_try_run_user_sync_completes_within_timeout():
    slot = PriorityJobSlot("test")
    status, result = slot.try_run_user_sync(lambda: 42, soft_timeout=1.0)
    assert status == "completed"
    assert result == 42


def test_try_run_user_sync_exception_returns_error():
    slot = PriorityJobSlot("test")

    def _boom():
        raise ValueError("boom")

    status, result = slot.try_run_user_sync(_boom, soft_timeout=1.0)
    assert status == "completed_with_error"
    assert isinstance(result, ValueError)


def test_try_run_user_sync_timeout_returns_promoted():
    """soft_timeout 超過時: worker は既に起動済みで裏で継続中 → "promoted"。"""
    slot = PriorityJobSlot("test")
    release = threading.Event()

    def _slow_fn():
        release.wait(timeout=5.0)
        return "done"

    status, result = slot.try_run_user_sync(_slow_fn, soft_timeout=0.2)
    assert status == "promoted"
    assert result is None

    # promoted ならスロットはまだ占有中 (worker 実行中)
    assert slot.is_running is True

    # 解放してバックグラウンド完了を待つ
    release.set()
    time.sleep(0.5)
    assert slot.is_running is False


def test_try_run_user_sync_when_slot_busy_returns_slot_busy():
    """他ジョブがスロット占有中: worker 未起動 → "slot_busy"。
    呼び出し側ジョブは実行されていない。
    """
    slot = PriorityJobSlot("test")
    bg_release = threading.Event()
    bg_started = threading.Event()

    def _bg_fn():
        bg_started.set()
        bg_release.wait(timeout=5.0)

    # バックグラウンドでジョブを走らせてスロットを占有
    threading.Thread(target=lambda: slot.try_run_scheduled(_bg_fn), daemon=True).start()
    bg_started.wait(timeout=1.0)

    # この間 try_run_user_sync は slot_busy を即返し、user の fn は呼ばれない
    new_called = threading.Event()
    status, result = slot.try_run_user_sync(
        lambda: new_called.set(), soft_timeout=0.1
    )
    assert status == "slot_busy"
    # 重要: user ジョブは実行されていないことを確認
    assert not new_called.is_set()

    bg_release.set()
    time.sleep(0.3)
    # bg 終了後も user ジョブは勝手に実行されない (呼び出し側が spawn_user_background する想定)
    assert not new_called.is_set()


# ── spawn_user_background ────────────────────────────────────


def test_spawn_user_background_runs_and_calls_callback():
    slot = PriorityJobSlot("test")
    completed = threading.Event()
    received_result = []

    def _fn():
        return 99

    def _on_complete(result, error):
        received_result.append((result, error))
        completed.set()

    assert slot.spawn_user_background(_fn, on_complete=_on_complete) is True
    assert completed.wait(timeout=2.0)
    assert received_result == [(99, None)]


def test_spawn_user_background_rejects_when_queue_full():
    slot = PriorityJobSlot("test")
    release_first = threading.Event()
    release_queued = threading.Event()
    first_started = threading.Event()

    def _first_fn():
        first_started.set()
        release_first.wait(timeout=5.0)

    def _queued_fn():
        release_queued.wait(timeout=5.0)

    # 1件目はスロットを占有して実行
    threading.Thread(target=lambda: slot.try_run_scheduled(_first_fn), daemon=True).start()
    first_started.wait(timeout=1.0)

    # 2件目は spawn_user_background で裏に置く (キュー深さ1)
    assert slot.spawn_user_background(_queued_fn, on_complete=lambda r, e: None) is True

    # 3件目はキュー溢れで False
    assert slot.spawn_user_background(lambda: None, on_complete=lambda r, e: None) is False

    # 後始末
    release_first.set()
    time.sleep(0.2)
    release_queued.set()
    time.sleep(0.2)


def test_spawn_user_background_exception_calls_callback_with_error():
    slot = PriorityJobSlot("test")
    completed = threading.Event()
    received = []

    def _boom():
        raise RuntimeError("explode")

    def _on_complete(result, error):
        received.append((result, error))
        completed.set()

    slot.spawn_user_background(_boom, on_complete=_on_complete)
    assert completed.wait(timeout=2.0)
    assert received[0][0] is None
    assert isinstance(received[0][1], RuntimeError)


# ── run_user_blocking ────────────────────────────────────────


def test_run_user_blocking_waits_for_slot():
    slot = PriorityJobSlot("test")
    release_first = threading.Event()
    first_started = threading.Event()
    blocking_result = []

    def _first():
        first_started.set()
        release_first.wait(timeout=5.0)
        return "first_done"

    def _second():
        return "second_done"

    # 先行ジョブがスロットを占有
    threading.Thread(target=lambda: slot.try_run_scheduled(_first), daemon=True).start()
    first_started.wait(timeout=1.0)

    # run_user_blocking は先行完了まで待って実行
    def _call_blocking():
        result = slot.run_user_blocking(_second)
        blocking_result.append(result)

    thread = threading.Thread(target=_call_blocking, daemon=True)
    thread.start()

    # 先行を解放
    time.sleep(0.1)
    release_first.set()

    thread.join(timeout=2.0)
    assert blocking_result == ["second_done"]


# ── run_user_blocking_with_timeout ─────────────────────────────


def test_blocking_with_timeout_empty_slot_returns_completed():
    """空きスロットなら即取得して結果を返す。"""
    slot = PriorityJobSlot("test")

    def _fn() -> str:
        return "ok"

    status, result = slot.run_user_blocking_with_timeout(_fn, timeout=1.0)
    assert status == "completed"
    assert result == "ok"


def test_blocking_with_timeout_propagates_exception_status():
    """fn が例外を投げたら ('completed_with_error', exc) を返し、スロットは解放される。"""
    slot = PriorityJobSlot("test")

    def _bad_fn() -> str:
        raise ValueError("boom")

    status, result = slot.run_user_blocking_with_timeout(_bad_fn, timeout=1.0)
    assert status == "completed_with_error"
    assert isinstance(result, ValueError)
    # スロットは解放されている → 次の呼び出しが通る
    second_status, second_result = slot.run_user_blocking_with_timeout(
        lambda: "second", timeout=1.0,
    )
    assert second_status == "completed"
    assert second_result == "second"


def test_blocking_with_timeout_waits_for_busy_slot_then_runs():
    """スロット占有中は完了まで待ってから順次実行する。"""
    slot = PriorityJobSlot("test")
    release_first = threading.Event()
    first_started = threading.Event()

    def _slow_first() -> str:
        first_started.set()
        release_first.wait(timeout=5.0)
        return "first_done"

    # 先行ジョブを別スレッドで開始
    threading.Thread(
        target=lambda: slot.run_user_blocking(_slow_first),
        daemon=True,
    ).start()
    first_started.wait(timeout=1.0)

    second_result: list = []

    def _call_blocking() -> None:
        status, result = slot.run_user_blocking_with_timeout(
            lambda: "second_done", timeout=2.0,
        )
        second_result.append((status, result))

    waiter = threading.Thread(target=_call_blocking, daemon=True)
    waiter.start()

    # まだ第二は走っていない (第一が解放されていない)
    time.sleep(0.1)
    assert second_result == []

    release_first.set()
    waiter.join(timeout=2.0)
    assert second_result == [("completed", "second_done")]


def test_blocking_with_timeout_returns_timeout_when_slot_stays_busy():
    """timeout 内にスロットが空かなければ ('timeout', None) を返す。"""
    slot = PriorityJobSlot("test")
    release = threading.Event()
    started = threading.Event()

    def _slow_fn() -> None:
        started.set()
        release.wait(timeout=5.0)

    threading.Thread(
        target=lambda: slot.run_user_blocking(_slow_fn),
        daemon=True,
    ).start()
    started.wait(timeout=1.0)

    status, result = slot.run_user_blocking_with_timeout(
        lambda: "should_not_run", timeout=0.2,
    )
    assert status == "timeout"
    assert result is None

    # 後始末
    release.set()
