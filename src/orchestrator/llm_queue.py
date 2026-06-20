"""LLM ジョブキュー + dispatcher (spec §6.1)。

全 LLM ジョブ (news 分析 / technical 分析 / planning / execution-opinion) を
優先度付きキューに enqueue し、単一 worker スレッドが逐次取り出して実行する。
worker=1 固定 (§4.2 sequential-by-design)。将来並列化は worker 数増で対応。

優先度: planning > technical > news (§5.1.1)。starvation 防止 (stale 昇格 /
max planning per window) は後続 plan で追加する。
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

# 小さいほど高優先 (PriorityQueue は最小値を先に取り出す)。
_PRIORITY = {"planning": 0, "execution": 1, "technical": 2, "news": 3}
_DEFAULT_PRIORITY = 9


@dataclass(order=True)
class _PrioritizedJob:
    priority: int
    seq: int
    job: "LlmJob" = field(compare=False)


@dataclass
class LlmJob:
    """1 件の LLM ジョブ。

    kind: 'planning' | 'execution' | 'technical' | 'news' — 優先度決定に使う。
    fn:   実際の処理 (引数なし呼び出し。クロージャで context を束ねる)。
    label: ログ・trace 用の識別子。
    """
    kind: str
    fn: Callable[[], object]
    label: str = ""


class LlmDispatcher:
    """優先度キュー + 単一 worker スレッド。

    start() で worker を起動し、stop() で停止する。enqueue() はいつでも可。
    wait_idle() はキューが空 + worker idle になるまでブロックする (テスト用)。
    """

    def __init__(self) -> None:
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    def enqueue(self, job: LlmJob) -> None:
        """job を優先度キューに積む。`queue.join()` 用の未完了カウントが増える。"""
        priority = _PRIORITY.get(job.kind, _DEFAULT_PRIORITY)
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        # PriorityQueue.put は内部で unfinished_tasks を +1 する。対応する
        # task_done を _run の finally で必ず 1 回呼ぶ (例外でも) ことで join が成立する。
        self._q.put(_PrioritizedJob(priority=priority, seq=seq, job=job))

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="llm-dispatcher", daemon=True
        )
        self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item: _PrioritizedJob = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                item.job.fn()
            except Exception:
                logger.exception(f"[LLM-QUEUE] job {item.job.label!r} raised")
            finally:
                # get 1 回につき task_done 1 回 (例外でも必ず)。これが無いと
                # join() が永久ブロックする。実行中は unfinished_tasks>0 なので
                # wait_idle は戻らない (= race のない idle 判定)。
                self._q.task_done()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """全 enqueue 済み job が task_done されるまで待つ。

        `queue.join()` を別スレッドで回し timeout 付きで待機する。実行中 job が
        ある間は unfinished_tasks>0 のため戻らない (empty 判定 race を持たない)。

        Returns: timeout 内に全 job 完了で True、超過で False。
        """
        done = threading.Event()

        def _joiner() -> None:
            self._q.join()
            done.set()

        threading.Thread(target=_joiner, name="llm-dispatcher-join", daemon=True).start()
        return done.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
