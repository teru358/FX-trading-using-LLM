"""PriorityJobSlot: LLMリソース用の優先度付き単一スロット。

ユーザー起動ジョブ (POST /ask 等) はスロット競合時に
バックグラウンドキューに1件だけ入れられる。定期ジョブは競合時にスキップ。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

UserSyncStatus = Literal["completed", "completed_with_error", "promoted", "slot_busy"]
UserBlockingStatus = Literal["completed", "completed_with_error", "timeout"]


class PriorityJobSlot:
    """LLM を使うジョブ全体の排他制御スロット。

    - try_run_scheduled: 定期ジョブ用、占有中ならスキップ
    - try_run_user_sync: ユーザー同期用、soft_timeout 内完了で同期返答、超過で非同期化
    - spawn_user_background: ユーザー非同期用、キュー深さ1 のバックグラウンド実行
    - run_user_blocking: CLI 用、完了まで同期 blocking
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._slot_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None
        self._waiting_user_job = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def started_at(self) -> datetime | None:
        with self._state_lock:
            return self._started_at

    @property
    def waiting_user_job(self) -> bool:
        with self._state_lock:
            return self._waiting_user_job

    # ── 内部ヘルパー ────────────────────────────────────────

    def _mark_started(self) -> None:
        with self._state_lock:
            self._running = True
            self._started_at = datetime.now()

    def _mark_finished(self) -> None:
        with self._state_lock:
            self._running = False
            self._started_at = None

    # ── 定期ジョブ用 ────────────────────────────────────────

    def try_run_scheduled(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """定期ジョブを実行する。スロット占有中ならスキップ (False)。"""
        if not self._slot_lock.acquire(blocking=False):
            logger.warning(f"[SLOT] {self._name}: scheduled job skipped (slot busy)")
            return False
        try:
            self._mark_started()
            start = time.monotonic()
            try:
                fn(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - start
                logger.info(f"[SLOT] {self._name}: scheduled job done in {elapsed:.1f}s")
                self._mark_finished()
            return True
        finally:
            self._slot_lock.release()

    # ── ユーザー同期用 ──────────────────────────────────────

    def try_run_user_sync(
        self,
        fn: Callable[..., Any],
        *args: Any,
        soft_timeout: float,
        on_promoted_complete: Callable[[Any, Exception | None], None] | None = None,
        **kwargs: Any,
    ) -> tuple[UserSyncStatus, Any]:
        """ユーザージョブを同期実行試行。

        Args:
            on_promoted_complete: promoted 経路で worker が完了した時に呼ばれる
                コールバック (result, error)。HTTP レスポンスを先に返した後、
                Discord 通知等で最終結果を届けるために使う。

        戻り値:
            ("completed", result)            → soft_timeout 内に正常完了
            ("completed_with_error", exc)    → soft_timeout 内に例外完了
            ("promoted", None)               → soft_timeout 超過 → ジョブはバックグラウンドで継続中
                                               on_promoted_complete 指定時は完了時にそれが呼ばれる
            ("slot_busy", None)              → 他ジョブが占有中で worker 未起動
                                               呼び出し側は spawn_user_background で再投入する必要あり
        """
        # スロット取得を試みる
        if not self._slot_lock.acquire(blocking=False):
            # 既に占有中 → worker 未起動、呼び出し側で spawn_user_background を呼ぶ
            return ("slot_busy", None)

        # 取れた → 別スレッドで実行し soft_timeout 待機
        result_holder: dict[str, Any] = {}
        done_event = threading.Event()

        def _worker() -> None:
            try:
                result_holder["value"] = fn(*args, **kwargs)
                result_holder["error"] = None
            except Exception as e:
                result_holder["value"] = None
                result_holder["error"] = e
            finally:
                done_event.set()

        self._mark_started()
        start = time.monotonic()
        worker = threading.Thread(target=_worker, name=f"slot-sync-{self._name}", daemon=True)
        worker.start()

        if done_event.wait(timeout=soft_timeout):
            # 時間内に完了
            elapsed = time.monotonic() - start
            logger.info(f"[SLOT] {self._name}: user sync done in {elapsed:.1f}s")
            self._mark_finished()
            self._slot_lock.release()
            if result_holder["error"] is not None:
                return ("completed_with_error", result_holder["error"])
            return ("completed", result_holder["value"])
        else:
            # 時間切れ → 実行は継続、呼び出し側に accepted を返す
            # スレッドはまだロックを握っているので、スレッド内で解放する
            logger.info(
                f"[SLOT] {self._name}: user sync exceeded soft_timeout={soft_timeout}s, "
                f"promoting to background"
            )

            def _cleanup_after_worker() -> None:
                worker.join()
                elapsed2 = time.monotonic() - start
                logger.info(f"[SLOT] {self._name}: promoted background done in {elapsed2:.1f}s")
                self._mark_finished()
                self._slot_lock.release()
                if on_promoted_complete is not None:
                    try:
                        on_promoted_complete(
                            result_holder.get("value"),
                            result_holder.get("error"),
                        )
                    except Exception as cb_err:
                        logger.warning(
                            f"[SLOT] {self._name}: on_promoted_complete raised: {cb_err}"
                        )

            threading.Thread(target=_cleanup_after_worker, daemon=True).start()
            return ("promoted", None)

    # ── ユーザー非同期用 ────────────────────────────────────

    def spawn_user_background(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_complete: Callable[[Any, Exception | None], None],
        **kwargs: Any,
    ) -> bool:
        """ユーザージョブを裏で実行。既にキュー待機中なら False。

        on_complete は実行完了後 (成功でもエラーでも) に呼ばれる。
        """
        with self._state_lock:
            if self._waiting_user_job:
                logger.warning(
                    f"[SLOT] {self._name}: user background queue full, rejecting new job"
                )
                return False
            self._waiting_user_job = True

        def _worker() -> None:
            try:
                # ロック取得まで待機
                with self._slot_lock:
                    with self._state_lock:
                        self._waiting_user_job = False
                    self._mark_started()
                    start = time.monotonic()
                    try:
                        result = fn(*args, **kwargs)
                        error = None
                    except Exception as e:
                        result = None
                        error = e
                    finally:
                        elapsed = time.monotonic() - start
                        logger.info(
                            f"[SLOT] {self._name}: user background done in {elapsed:.1f}s"
                        )
                        self._mark_finished()
                try:
                    on_complete(result, error)
                except Exception as cb_err:
                    logger.error(
                        f"[SLOT] {self._name}: on_complete callback failed: {cb_err}",
                        exc_info=True,
                    )
            except Exception as outer:
                logger.error(
                    f"[SLOT] {self._name}: user background worker crashed: {outer}",
                    exc_info=True,
                )
                with self._state_lock:
                    self._waiting_user_job = False

        threading.Thread(target=_worker, name=f"slot-bg-{self._name}", daemon=True).start()
        return True

    # ── CLI 用: 完了まで同期待機 ────────────────────────────

    def run_user_blocking(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """CLI 用: スロット取得を待って同期実行、結果を返す。タイムアウトなし。"""
        with self._slot_lock:
            self._mark_started()
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - start
                logger.info(f"[SLOT] {self._name}: user blocking done in {elapsed:.1f}s")
                self._mark_finished()

    # ── REST API 用: タイムアウト付き同期待機 ─────────────────

    def run_user_blocking_with_timeout(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float,
        **kwargs: Any,
    ) -> tuple[UserBlockingStatus, Any]:
        """スロット取得を最大 ``timeout`` 秒待ち、取れたら同期実行する。

        Discord 通知 OFF の REST API から、走行中ジョブの完了を待ってから
        順次処理したいケースで使う。HTTP リクエストを長時間ブロックするので
        必ず合理的な ``timeout`` を指定すること。

        戻り値:
            ``("completed", result)``         → スロット取得 + 実行成功
            ``("completed_with_error", exc)`` → スロット取得 + 実行例外
            ``("timeout", None)``             → ``timeout`` 内にスロット取得できず
        """
        if not self._slot_lock.acquire(timeout=timeout):
            logger.info(
                f"[SLOT] {self._name}: user blocking gave up after {timeout:.0f}s "
                f"(slot still busy)"
            )
            return ("timeout", None)
        try:
            self._mark_started()
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed = time.monotonic() - start
                logger.info(
                    f"[SLOT] {self._name}: user blocking-with-timeout done in {elapsed:.1f}s"
                )
                return ("completed", result)
            except Exception as e:
                elapsed = time.monotonic() - start
                logger.warning(
                    f"[SLOT] {self._name}: user blocking-with-timeout failed in "
                    f"{elapsed:.1f}s: {e}"
                )
                return ("completed_with_error", e)
            finally:
                self._mark_finished()
        finally:
            self._slot_lock.release()
