"""OrchestratorRuntime (spec §4) — 非LLM ランタイム。

Phase 1 foundation: planning loop と watch loop の 2 ループの骨格。
- planning loop: Context Builder で decision_snapshot を materialize し、
  direct_hold decision を記録する (PlannerAgent / plan 作成は later plan)。
- watch loop: active plan を走査し freshness を記録する。**発注は行わない**
  (entry_conditions の実評価・執行は later plan)。

両ループは orchestrator.enabled が true のときのみ start() で起動する。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from src.config.schema import OrchestratorConfig
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import ContextBuilder, QuoteSnapshot
from src.utils.clock import db_now

logger = logging.getLogger(__name__)

QuoteProvider = Callable[[str], QuoteSnapshot]


class OrchestratorRuntime:
    """2 ループ (planning / watch) を駆動する非LLM ランタイム骨格。"""

    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        orch_store: OrchestratorStore,
        context_builder: ContextBuilder,
        pairs: list[str],
        quote_provider: QuoteProvider,
    ) -> None:
        self._config = config
        self._orch = orch_store
        self._ctx = context_builder
        self._pairs = pairs
        self._quote_provider = quote_provider
        self._stop = threading.Event()
        self._planning_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None

    # ── 1 サイクル分の処理 (テスト・手動駆動可) ─────────────────

    def run_planning_cycle(self, now: datetime | None = None) -> None:
        """全 pair について planning を 1 周する。

        Phase 1: snapshot を materialize し direct_hold を記録するのみ。
        PlannerAgent による trade_plan 作成は later plan で差し込む。
        """
        now = now or db_now()
        for pair in self._pairs:
            quote = self._quote_provider(pair)
            run_id = self._orch.start_run(
                "OrchestratorRuntime",
                pair=pair,
                trigger_type="planning_cycle",
                trade_horizon=self._config.policy.trade_horizon,
            )
            try:
                ctx = self._ctx.build(pair=pair, now=now, quote=quote)
                # later plan: ここで PlannerAgent を呼び trade_plan を立/改/無効化する。
                # Phase 1 は機会判断を行わず direct_hold を記録する。
                self._orch.record_decision(
                    run_id=run_id,
                    snapshot_id=ctx["snapshot_id"],
                    pair=pair,
                    decision_type="direct_hold",
                    decision="hold",
                    reasoning_summary="phase1 observe: no planning agent wired yet",
                    trade_horizon=self._config.policy.trade_horizon,
                )
                self._orch.finish_run(run_id, status="ok")
            except Exception as exc:
                logger.exception(f"[ORCH] planning cycle failed for {pair}")
                self._orch.finish_run(
                    run_id, status="failed",
                    error_type=type(exc).__name__, error_message=str(exc),
                )

    def run_watch_cycle(self) -> list[int]:
        """active plan を走査する。

        Phase 1: freshness を記録するのみ。entry_conditions の評価・執行は
        later plan。**発注は行わないため triggered は常に空を返す。**

        Returns: triggered plan_id のリスト (Phase 1 では常に [])。
        """
        triggered: list[int] = []
        for pair in self._pairs:
            plans = self._orch.get_active_plans(pair)
            for plan in plans:
                # later plan: ここで entry_conditions を tick 評価し、成立なら
                # plan freshness 再検証 → RiskGateWorker → broker gate → 発注。
                # Phase 1 は観測のみ。
                self._orch.record_freshness(
                    snapshot_id=plan.snapshot_id,
                    pair=pair,
                    issues=[],
                )
        return triggered

    # ── ループ駆動 (enabled 時のみ) ─────────────────────────────

    def start(self) -> None:
        """enabled なら 2 ループスレッドを起動する。disabled なら何もしない。

        既に起動済み (スレッド生存中) の再 start() は no-op。これが無いと二重 start で
        スレッドが 4 本になり、最初の 2 本が参照を失って stop() で join 不能になる。
        """
        if not self._config.enabled:
            logger.info("[ORCH] orchestrator disabled — loops not started")
            return
        if self._planning_thread is not None and self._planning_thread.is_alive():
            logger.warning("[ORCH] start() called while already running — ignored")
            return
        self._stop.clear()
        self._planning_thread = threading.Thread(
            target=self._planning_loop, name="orch-planning", daemon=True
        )
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="orch-watch", daemon=True
        )
        self._planning_thread.start()
        self._watch_thread.start()
        logger.info(f"[ORCH] started (mode={self._config.mode}, pairs={self._pairs})")

    def stop(self) -> None:
        self._stop.set()
        for t in (self._planning_thread, self._watch_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._planning_thread = None
        self._watch_thread = None

    def _planning_loop(self) -> None:
        wait = self._config.market_state.normal_seconds
        while not self._stop.is_set():
            try:
                self.run_planning_cycle()
            except Exception:
                logger.exception("[ORCH] planning loop iteration failed")
            self._stop.wait(timeout=wait)

    def _watch_loop(self) -> None:
        # Phase 1: tick stream 未接続のため固定の短間隔でポーリングする。
        # later plan で websocket tick 駆動に置き換える (§5.5)。
        while not self._stop.is_set():
            try:
                self.run_watch_cycle()
            except Exception:
                logger.exception("[ORCH] watch loop iteration failed")
            self._stop.wait(timeout=1.0)
