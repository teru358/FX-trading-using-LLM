"""LLM job queue + dispatcher (spec §6.1) のテスト。"""
from __future__ import annotations

import threading
import time

from src.orchestrator.llm_queue import LlmJob, LlmDispatcher


def test_jobs_run_in_priority_order() -> None:
    """planning > technical > news の順で実行される (enqueue 順に依らない)。"""
    executed: list[str] = []
    lock = threading.Lock()

    def make_fn(name: str):
        def _fn() -> None:
            with lock:
                executed.append(name)
        return _fn

    dispatcher = LlmDispatcher()
    # enqueue を news → technical → planning の順 (= 優先度の逆順) で行う。
    dispatcher.enqueue(LlmJob(kind="news", fn=make_fn("news"), label="n1"))
    dispatcher.enqueue(LlmJob(kind="technical", fn=make_fn("technical"), label="t1"))
    dispatcher.enqueue(LlmJob(kind="planning", fn=make_fn("planning"), label="p1"))

    dispatcher.start()
    dispatcher.wait_idle(timeout=5.0)
    dispatcher.stop()

    # planning が最初。news / technical は planning の後 (technical > news)。
    assert executed[0] == "planning"
    assert executed.index("technical") < executed.index("news")


def test_single_worker_serializes_execution() -> None:
    """worker=1 のため 2 つの job が同時に走らない。"""
    concurrent = 0
    max_concurrent = 0
    lock = threading.Lock()

    def busy_fn() -> None:
        nonlocal concurrent, max_concurrent
        with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        time.sleep(0.05)
        with lock:
            concurrent -= 1

    dispatcher = LlmDispatcher()
    for i in range(4):
        dispatcher.enqueue(LlmJob(kind="planning", fn=busy_fn, label=f"p{i}"))
    dispatcher.start()
    dispatcher.wait_idle(timeout=5.0)
    dispatcher.stop()

    assert max_concurrent == 1


def test_wait_idle_blocks_until_running_job_finishes() -> None:
    """wait_idle() は実行中の job が完了するまで戻らない (queue.join() ベース)。

    _idle Event を empty 判定で set する旧実装は、enqueue と worker の empty
    判定が競合すると queued job があるのに idle に見える穴があった。queue.join()
    + unfinished-tasks カウントで「全 task が task_done されるまで戻らない」を保証する。
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_fn() -> None:
        started.set()
        release.wait(timeout=5.0)   # テストが解放するまでブロック
        finished.set()

    dispatcher = LlmDispatcher()
    dispatcher.enqueue(LlmJob(kind="planning", fn=slow_fn, label="slow"))
    dispatcher.start()

    assert started.wait(timeout=2.0)            # job 実行開始を確認
    # job 実行中: wait_idle は短い timeout では戻らない (False)
    assert dispatcher.wait_idle(timeout=0.2) is False
    assert not finished.is_set()

    release.set()                               # job を完了させる
    assert dispatcher.wait_idle(timeout=2.0) is True  # 完了後は戻る
    assert finished.is_set()
    dispatcher.stop()
