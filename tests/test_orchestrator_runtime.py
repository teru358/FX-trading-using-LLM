"""OrchestratorRuntime のループ・trace 記録テスト (observe mode, 発注なし)。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime


@pytest.fixture
def runtime(tmp_path: Path) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
    )


def test_run_planning_cycle_records_direct_hold(runtime: OrchestratorRuntime) -> None:
    """1 planning cycle で snapshot + agent_run + direct_hold decision が残る。"""
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    # agent_run が ok で finish している
    run = runtime._orch.get_run(1)
    assert run is not None
    assert run.status == "ok"
    assert run.finished_at is not None

    # direct_hold decision が記録されている
    dec = runtime._orch.get_decision(1)
    assert dec is not None
    assert dec.decision_type == "direct_hold"
    assert dec.pair == "USDJPY=X"
    assert dec.plan_id is None

    # trace graph が繋がっている: agent_run.snapshot_id == decision.snapshot_id
    # (start_run 後に attach_snapshot で紐付けられ、NULL のまま残らない)。
    assert run.snapshot_id is not None
    assert run.snapshot_id == dec.snapshot_id


def test_run_watch_cycle_no_active_plans_is_noop(runtime: OrchestratorRuntime) -> None:
    """active plan が無ければ watch cycle は何も執行せず例外も出さない。"""
    triggered = runtime.run_watch_cycle()
    assert triggered == []


def test_run_watch_cycle_records_freshness_for_active_plan(
    runtime: OrchestratorRuntime,
) -> None:
    """active plan があれば freshness を記録するが、観測モードでは執行しない。"""
    snap_id = runtime._orch.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    runtime._orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap_id, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=1,
    )
    triggered = runtime.run_watch_cycle()
    # observe mode: 条件評価は later plan。ここでは執行 (order) が無いことを保証する。
    assert triggered == []
    # freshness 行が実際に記録されていることを検証する (record_freshness が消えても
    # triggered==[] は通るため、保存自体を assert してテストを名前通りにする)。
    fresh = runtime._orch.get_freshness_for_snapshot(snap_id)
    assert fresh is not None
    assert fresh.snapshot_id == snap_id
    assert fresh.pair == "USDJPY=X"


def test_start_is_noop_when_disabled(runtime: OrchestratorRuntime) -> None:
    """enabled=false (既定) なら start() でスレッドが立たない。"""
    runtime.start()
    assert runtime._planning_thread is None
    assert runtime._watch_thread is None
    runtime.stop()  # 何もしないことを確認 (例外が出ない)


def test_run_planning_cycle_marks_run_failed_on_error(tmp_path: Path) -> None:
    """build() が例外を投げても run は failed で finish され、dangling run を残さない。"""
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)

    class _BoomBuilder:
        def build(self, *, pair, now, quote):
            raise RuntimeError("boom")

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=_BoomBuilder(),
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
    )
    # 例外は cycle 内で握られ、ループには伝播しない (raise しない)
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    run = orch.get_run(1)
    assert run is not None
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.error_type == "RuntimeError"
    assert run.error_message == "boom"
    # 失敗時は decision を記録しない (build より後の record_decision に到達しない)
    assert orch.get_decision(1) is None


def test_start_spawns_threads_when_enabled_and_stop_joins(tmp_path: Path) -> None:
    """enabled=true なら start() で 2 スレッドが立ち、stop() で停止・join される。"""
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    cfg = OrchestratorConfig(enabled=True)
    runtime = OrchestratorRuntime(
        config=cfg,
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
    )
    runtime.start()
    try:
        assert runtime._planning_thread is not None
        assert runtime._watch_thread is not None
        assert runtime._planning_thread.is_alive()
        assert runtime._watch_thread.is_alive()
        # 二重 start() は no-op (スレッドを増やさない): 同じスレッドオブジェクトのまま
        planning_before = runtime._planning_thread
        runtime.start()
        assert runtime._planning_thread is planning_before
    finally:
        runtime.stop()
    assert runtime._planning_thread is None
    assert runtime._watch_thread is None


def test_run_planning_cycle_quote_failure_marks_failed_and_continues(tmp_path: Path) -> None:
    """quote 取得が落ちても failed run を残し、残りペアの planning は止めない。

    quote_provider は落ちやすい入力。start_run 後の try 内で取得するため、
    1 ペア目で例外 → そのペアは failed run + decision 無し、2 ペア目は正常処理される。
    """
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def flaky_quote_provider(pair: str) -> QuoteSnapshot:
        if pair == "USDJPY=X":
            raise RuntimeError("price feed down")
        return QuoteSnapshot(
            bid=1.08, ask=1.0802, mid=1.0801, spread=0.0002,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X", "EURUSD=X"],
        quote_provider=flaky_quote_provider,
    )
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    # run 1 (USDJPY=X): quote 失敗でも failed run が残る (dangling しない)
    run1 = orch.get_run(1)
    assert run1 is not None
    assert run1.pair == "USDJPY=X"
    assert run1.status == "failed"
    assert run1.error_type == "RuntimeError"
    assert run1.snapshot_id is None      # build 前に落ちたので snapshot 無し

    # run 2 (EURUSD=X): 1 ペア目の失敗に巻き込まれず正常完了
    run2 = orch.get_run(2)
    assert run2 is not None
    assert run2.pair == "EURUSD=X"
    assert run2.status == "ok"
    assert run2.snapshot_id is not None

    # 記録された decision は run2 (EURUSD) のもの 1 件のみ。run1 は record_decision に
    # 到達しないため、その decision は存在しない (decision_id は run_id と独立採番なので
    # decision_id=1 が run2 のもの、decision_id=2 は存在しない)。
    dec = orch.get_decision(1)
    assert dec is not None
    assert dec.run_id == run2.run_id             # run2 に紐づく decision
    assert dec.pair == "EURUSD=X"
    assert dec.decision_type == "direct_hold"
    assert dec.snapshot_id == run2.snapshot_id   # run2 の trace graph も繋がっている
    assert orch.get_decision(2) is None          # 2 件目の decision は無い (run1 は未記録)


def test_planning_cycle_drives_pipeline_when_injected(tmp_path: Path) -> None:
    """pipeline 注入時は direct_hold stub でなく pipeline.run を駆動し ok で finish する。"""
    from src.orchestrator.planning_pipeline import PipelineResult

    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    calls: list[dict] = []

    class _FakePipeline:
        async def run(self, *, pair, context, run_id):
            calls.append({"pair": pair, "run_id": run_id, "snapshot_id": context["snapshot_id"]})
            return PipelineResult(outcome="direct_hold", decision_ids=[])

    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        pipeline=_FakePipeline(),
    )
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    # pipeline.run が build 済み context と run_id で呼ばれた
    assert len(calls) == 1
    assert calls[0]["pair"] == "USDJPY=X"
    run = orch.get_run(1)
    assert run is not None
    assert run.run_id == calls[0]["run_id"]
    assert run.snapshot_id == calls[0]["snapshot_id"]
    # outcome != failed → ok で finish
    assert run.status == "ok"
    # Phase1 stub の direct_hold は記録されない (pipeline が decision を担う)
    assert orch.get_decision(1) is None


def test_planning_cycle_marks_failed_when_pipeline_outcome_failed(tmp_path: Path) -> None:
    """pipeline が outcome='failed' を返したら run を failed で finish する。"""
    from src.orchestrator.planning_pipeline import PipelineResult

    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    class _FailingPipeline:
        async def run(self, *, pair, context, run_id):
            return PipelineResult(outcome="failed", error="SchemaParseError: boom")

    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        pipeline=_FailingPipeline(),
    )
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    run = orch.get_run(1)
    assert run is not None
    assert run.status == "failed"
    assert run.finished_at is not None
    # snapshot は作れている (fail-safe は plan を作らないだけで snapshot は残す)
    assert run.snapshot_id is not None


def test_planning_cycle_only_processes_detector_pairs(tmp_path: Path) -> None:
    """detector が返した pair のみ planning する (material 駆動)。"""
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    class _FakeDetector:
        def __init__(self, fire):
            self.fire = fire
            self.committed = []
            self.attempted = []
        def pairs_to_plan(self, now):
            return list(self.fire)
        def mark_committed(self, pair, now):
            self.committed.append(pair)
        def mark_attempted(self, pair, now):
            self.attempted.append(pair)

    detector = _FakeDetector(fire=["USDJPY=X"])
    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X", "EURUSD=X"],
        quote_provider=quote_provider,
        detector=detector,
    )
    runtime.run_planning_cycle(now=datetime(2026, 6, 20, 12, 0, 0))

    # USDJPY=X だけ planning される; EURUSD=X は detector が返さないので processed されない
    dec1 = orch.get_decision(1)
    assert dec1 is not None
    assert dec1.pair == "USDJPY=X"
    assert orch.get_decision(2) is None   # 2件目は無い
    # snapshot 作成まで到達した (成功) ので mark_committed が呼ばれる (mark_attempted でない)
    assert detector.committed == ["USDJPY=X"]
    assert detector.attempted == []
