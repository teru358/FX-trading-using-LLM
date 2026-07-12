"""LLM endpoint 単位の排他 gate (spec §2.M)。

llama-server (ローカル VRAM) への同時リクエストは ReadTimeout → circuit 誤 OPEN の
原因になるため、base_url 単位の threading.Semaphore(1) でリクエストを直列化する。
busy 中は待機する (skip しない — LLM への並列処理は非推奨のため待つのが正しい)。

threading.Semaphore を使うのは、系統 A (main スケジューラの各 job thread) と
系統 B (orchestrator planning thread) が別々のイベントループから呼ぶため
(asyncio.Lock は loop を跨げない)。async 側は worker thread 経由で取得し、待機中も
イベントループを塞がない。

cancellation-safe (codex 3巡目 #1): 取得待ち中に task が cancel されても worker
thread の sem.acquire() は止まらない。acquire future を shield し、cancel 時は
「取得完了したら即 release する」done callback を仕込んで re-raise する。

注意 (high fan-out 非対応): 各 waiter は取得待ちの間ずっと default
ThreadPoolExecutor のスレッドを 1 本消費する。多数の同時 caller から呼ぶと
pool を枯渇させるため使わないこと (現状は scheduler + planning thread の 2 経路想定)。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

_registry_lock = threading.Lock()
_gates: dict[str, threading.Semaphore] = {}

_WAIT_WARN_SECONDS = 30.0


def _gate_for(base_url: str) -> threading.Semaphore:
    key = (base_url or "").rstrip("/")
    with _registry_lock:
        if key not in _gates:
            _gates[key] = threading.Semaphore(1)
        return _gates[key]


async def _acquire_cancellation_safe(sem: threading.Semaphore) -> None:
    """worker thread で sem を取得する。cancel されたら取得済/取得予定分を必ず返す。"""
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, sem.acquire)
    try:
        await asyncio.shield(fut)
    except asyncio.CancelledError:
        # worker thread は止められない。取得が完了し次第 release して返す。
        fut.add_done_callback(lambda f: sem.release())
        raise


@contextlib.asynccontextmanager
async def endpoint_gate(base_url: str):
    """base_url 単位でリクエストを直列化する async context manager。"""
    sem = _gate_for(base_url)
    start = time.monotonic()
    await _acquire_cancellation_safe(sem)
    waited = time.monotonic() - start
    if waited >= _WAIT_WARN_SECONDS:
        logger.warning(f"[GATE] {base_url}: waited {waited:.1f}s for LLM slot")
    try:
        yield
    finally:
        sem.release()
