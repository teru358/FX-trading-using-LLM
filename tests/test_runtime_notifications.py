"""OrchestratorRuntime → ShadowNotifier 配線テスト (Phase 5 Task 5.2)。

shadow trigger / plan create / plan reject / hindsight evaluated の各イベントで
ShadowNotifier の対応メソッドが呼ばれること、未注入 (None) でも従来どおり動くこと、
通知例外が deterministic path を壊さないことを検証する。実 Discord は叩かない。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.analysis.price_analyzer import PriceAnalysis
from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.planning_pipeline import PipelineResult
from src.orchestrator.runtime import OrchestratorRuntime

NOW = datetime(2026, 6, 21, 9, 0, 0)
FUTURE = NOW + timedelta(days=1)


class RecordingShadowNotifier:
    """ShadowNotifier の duck-typed スパイ。呼ばれたイベントを記録する。"""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.events: list[tuple[str, object]] = []
        self._raise_on = raise_on

    async def notify_plan_created(self, info) -> None:
        self._maybe_raise("plan_created")
        self.events.append(("plan_created", info))

    async def notify_plan_rejected(self, *, pair, reason) -> None:
        self._maybe_raise("plan_rejected")
        self.events.append(("plan_rejected", (pair, reason)))

    async def notify_plan_superseded(self, *, pair, old_plan_id, new_plan_id) -> None:
        self._maybe_raise("plan_superseded")
        self.events.append(("plan_superseded", (pair, old_plan_id, new_plan_id)))

    async def notify_shadow_trigger(self, info) -> None:
        self._maybe_raise("shadow_trigger")
        self.events.append(("shadow_trigger", info))

    async def notify_hindsight_evaluated(self, info) -> None:
        self._maybe_raise("hindsight")
        self.events.append(("hindsight", info))

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise RuntimeError(f"boom in {name}")

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


class FakePipeline:
    """plan_create を返す最小 pipeline。runtime の planning 配線テスト用。"""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result

    async def run(self, *, pair, context, run_id) -> PipelineResult:
        return self._result


def _seed_ok_technical(db: Path) -> None:
    AnalysisStore(db).add_snapshot(
        PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.7,
            entry_zone=(149.0, 151.0), reasoning_summary="seed",
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def _make_runtime(
    tmp_path: Path, *, notifier, mid: float = 150.10,
    pipeline=None, hindsight=None, seed_technical: bool = True,
) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    if seed_technical:
        _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - 0.005, ask=mid + 0.005, mid=mid, spread=0.01,
            source="test", observed_at=NOW,
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        pipeline=pipeline,
        hindsight_evaluator=hindsight,
        shadow_notifier=notifier,
    )


def _create_plan(orch: OrchestratorStore, *, entry: list[dict]) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=entry,
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=[], expires_at=FUTURE, created_by_run_id=run_id,
    )


# ── shadow trigger 配線 ───────────────────────────────────────


def test_shadow_trigger_notifies(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == [plan_id]
    assert "shadow_trigger" in notifier.kinds()
    _, info = next(e for e in notifier.events if e[0] == "shadow_trigger")
    assert info.pair == "USDJPY=X"
    assert info.direction == "long"
    assert info.plan_id == plan_id
    assert info.trigger_price == 150.10
    assert info.tp == 151.5 and info.sl == 149.4


def test_no_notifier_still_triggers(tmp_path: Path) -> None:
    """notifier 未注入でも従来どおり trigger 記録は動く (後方互換)。"""
    rt = _make_runtime(tmp_path, notifier=None, mid=150.10)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])
    assert rt.run_watch_cycle(now=NOW) == [plan_id]
    assert rt._orch.get_shadow_trigger(plan_id) is not None


def test_notifier_exception_does_not_break_trigger(tmp_path: Path) -> None:
    """通知が例外を投げても shadow trigger 記録は完了する (deterministic path 保護)。"""
    notifier = RecordingShadowNotifier(raise_on="shadow_trigger")
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])
    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == [plan_id]
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "triggered"
    assert rt._orch.get_shadow_trigger(plan_id) is not None


# ── planning 配線 (plan_create / reject) ─────────────────────


def test_plan_create_notifies(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    result = PipelineResult(
        outcome="plan_create", plan_id=1,
        direction="long", score=0.7, confidence=0.66,
        reason="created", superseded_plan_ids=[],
    )
    rt = _make_runtime(tmp_path, notifier=notifier, pipeline=FakePipeline(result))
    rt.run_planning_cycle(now=NOW)
    assert "plan_created" in notifier.kinds()
    _, info = next(e for e in notifier.events if e[0] == "plan_created")
    assert info.plan_id == 1 and info.direction == "long"


def test_plan_create_with_supersede_notifies_both(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    result = PipelineResult(
        outcome="plan_create", plan_id=9,
        direction="short", score=0.6, confidence=0.5,
        reason="x", superseded_plan_ids=[7],
    )
    rt = _make_runtime(tmp_path, notifier=notifier, pipeline=FakePipeline(result))
    rt.run_planning_cycle(now=NOW)
    assert "plan_created" in notifier.kinds()
    assert "plan_superseded" in notifier.kinds()
    _, sup = next(e for e in notifier.events if e[0] == "plan_superseded")
    assert sup == ("USDJPY=X", 7, 9)


def test_plan_reject_notifies(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    result = PipelineResult(outcome="reject", reason="risk reject: spread too wide")
    rt = _make_runtime(tmp_path, notifier=notifier, pipeline=FakePipeline(result))
    rt.run_planning_cycle(now=NOW)
    assert "plan_rejected" in notifier.kinds()
    _, payload = next(e for e in notifier.events if e[0] == "plan_rejected")
    assert payload == ("USDJPY=X", "risk reject: spread too wide")


def test_direct_hold_does_not_notify(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    result = PipelineResult(outcome="direct_hold")
    rt = _make_runtime(tmp_path, notifier=notifier, pipeline=FakePipeline(result))
    rt.run_planning_cycle(now=NOW)
    assert notifier.events == []


# ── hindsight 配線 ────────────────────────────────────────────


class StubHindsight:
    """trigger 後の hindsight を即評価して返すスタブ。"""

    def evaluate(self, **kwargs):
        from src.orchestrator.hindsight_evaluator import HindsightResult

        return HindsightResult(
            has_data=True, mfe_r=1.5, mae_r=-0.3, pnl_r=2.0,
            would_hit_tp=True, would_hit_sl=False,
            reasoning_summary="ok",
        )


def test_hindsight_evaluated_notifies(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    rt = _make_runtime(
        tmp_path, notifier=notifier, mid=150.10, hindsight=StubHindsight(),
    )
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])
    rt.run_watch_cycle(now=NOW)  # creates trigger + pending hindsight row

    # horizon を過ぎた時点で評価
    later = NOW + timedelta(days=2)
    rt.run_hindsight_cycle(now=later)

    assert "hindsight" in notifier.kinds()
    _, info = next(e for e in notifier.events if e[0] == "hindsight")
    assert info.pair == "USDJPY=X"
    assert info.pnl_r == 2.0
    assert info.would_hit_tp is True


# ── 通知 worker (ノンブロッキング) — Codex Medium#1 ───────────


class SlowNotifier(RecordingShadowNotifier):
    """notify が指定秒ブロックする notifier。ループ滞留検証用。"""

    def __init__(self, *, delay: float) -> None:
        super().__init__()
        self._delay = delay

    async def notify_shadow_trigger(self, info) -> None:
        time.sleep(self._delay)
        await super().notify_shadow_trigger(info)


def test_notifications_run_on_worker_thread_do_not_block_cycle(tmp_path: Path) -> None:
    """worker 起動時、遅い通知が watch cycle の戻りをブロックしない。

    通知は worker thread へ enqueue され、run_watch_cycle 自体は即座に返る。
    その後 worker が遅延付きで処理を完了する。
    """
    notifier = SlowNotifier(delay=0.5)
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    rt._start_notify_worker()
    try:
        t0 = time.monotonic()
        triggered = rt.run_watch_cycle(now=NOW)
        elapsed = time.monotonic() - t0

        assert triggered == [plan_id]
        # cycle は通知完了 (0.5s) を待たずに返る。
        assert elapsed < 0.3, f"cycle blocked on notification: {elapsed:.3f}s"

        # worker が最終的に通知を処理する。
        deadline = time.monotonic() + 3.0
        while not notifier.kinds() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "shadow_trigger" in notifier.kinds()
    finally:
        rt._stop_notify_worker()


def test_stop_notify_worker_drains_pending(tmp_path: Path) -> None:
    """_stop_notify_worker は queue の未処理分を drain してから止まる。"""
    notifier = RecordingShadowNotifier()
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    rt._start_notify_worker()
    rt.run_watch_cycle(now=NOW)
    assert triggered_ok(plan_id, rt)
    rt._stop_notify_worker()  # drain + join

    assert "shadow_trigger" in notifier.kinds()


def triggered_ok(plan_id: int, rt: OrchestratorRuntime) -> bool:
    return rt._orch.get_trade_plan(plan_id).status == "triggered"


def test_worker_thread_not_started_falls_back_to_sync(tmp_path: Path) -> None:
    """worker 未起動なら同期実行 (テスト・手動駆動の後方互換)。"""
    notifier = RecordingShadowNotifier()
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])
    rt.run_watch_cycle(now=NOW)
    # worker を起動していないので、戻り時点で既に同期処理済み。
    assert "shadow_trigger" in notifier.kinds()


def test_only_one_notify_worker_even_on_double_start(tmp_path: Path) -> None:
    notifier = RecordingShadowNotifier()
    rt = _make_runtime(tmp_path, notifier=notifier, mid=150.10)
    rt._start_notify_worker()
    rt._start_notify_worker()  # 2 回目は no-op
    try:
        alive = [
            t for t in threading.enumerate() if t.name == "orch-notify" and t.is_alive()
        ]
        assert len(alive) == 1
    finally:
        rt._stop_notify_worker()
