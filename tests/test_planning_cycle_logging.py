"""run_planning_cycle の start/result ログと start:result 1:1 契約テスト。

planning cycle が「何を判断したか」を [ORCH] planning start / [ORCH] planning result
の 1:1 で残すことを検証する。start ログは start_run 成功後にのみ出す (start_run が
落ちたら start=0/result=0)。正常経路は finish_run → result ログ → 通知 の順で、通知
失敗は result を error 化しない。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.material_landing import PlanningTarget
from src.orchestrator.planning_pipeline import PipelineResult
from src.orchestrator.runtime import OrchestratorRuntime
from src.utils.clock import db_now

NOW = db_now()


def _count(records, needle):
    return sum(1 for r in records if needle in r.getMessage())


def _decisions(records):
    return [r.getMessage() for r in records if "[ORCH] planning result" in r.getMessage()]


def _seed_ok_technical(db: Path, pair: str) -> None:
    """freshness を満たす ok technical snapshot を 1 件入れる (db_now 相対)。"""
    AnalysisStore(db).add_snapshot(
        TechnicalSnapshotData(
            pair=pair, direction_bias="long", bias_score=0.5, confidence=0.7,
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


class _StubPipeline:
    """run() が固定 PipelineResult を返す最小 pipeline stub。"""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result

    async def run(self, *, pair, context, run_id):  # noqa: D401 - stub
        return self._result


class _StubDetector:
    """pairs を PlanningTarget として返し、mark_* を no-op にする detector stub。"""

    def __init__(self, pairs: list[str]) -> None:
        self._pairs = pairs
        self.committed: list[str] = []
        self.attempted: list[str] = []

    def pairs_to_plan(self, now):
        return [PlanningTarget(pair=p, triggers=()) for p in self._pairs]

    def mark_committed(self, pair, now):
        self.committed.append(pair)

    def mark_attempted(self, pair, now):
        self.attempted.append(pair)


class _RaisingBuilder:
    """build が必ず例外を投げる context builder stub (context_build_failure)。"""

    def build(self, *, pair, now, quote):
        raise RuntimeError("ctx boom")


class _RaisingStore:
    """start_run が必ず例外を投げる orch_store ラッパ (start_run_failure)。

    それ以外の呼び出しは委譲する (実際には start_run で止まるので呼ばれない)。
    """

    def __init__(self, inner: OrchestratorStore) -> None:
        self._inner = inner

    def start_run(self, *args, **kwargs):
        raise RuntimeError("start_run boom")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _result_for(scenario: str) -> PipelineResult:
    if scenario == "direct_hold":
        return PipelineResult(outcome="direct_hold", reason="no edge")
    if scenario == "reject":
        return PipelineResult(outcome="reject", reason="rr too low")
    if scenario == "plan_create":
        return PipelineResult(
            outcome="plan_create", plan_id=42, direction="long",
            reason="entry setup", derived_rr=2.0, score=0.6, confidence=0.7,
        )
    if scenario == "pipeline_failed":
        return PipelineResult(outcome="failed", error="LLM timeout", reason="")
    raise AssertionError(f"no PipelineResult for scenario {scenario!r}")


@pytest.fixture
def planning_runtime_factory(tmp_path: Path):
    def _factory(scenario: str, *, pairs=None, notify_raises: bool = False):
        pairs = pairs or ["USDJPY=X"]
        db = tmp_path / "orch.db"
        orch = OrchestratorStore(db)
        for p in pairs:
            _seed_ok_technical(db, p)
        real_builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

        def quote_provider(pair: str) -> QuoteSnapshot:
            if scenario == "quote_failure":
                raise RuntimeError("quote boom")
            return QuoteSnapshot(
                bid=150.09, ask=150.11, mid=150.10, spread=0.02,
                source="test", observed_at=NOW,
            )

        builder = _RaisingBuilder() if scenario == "context_build_failure" else real_builder

        pipeline = None
        if scenario in ("direct_hold", "reject", "plan_create", "pipeline_failed"):
            pipeline = _StubPipeline(_result_for(scenario))
        # phase1_observe / quote_failure / context_build_failure / start_run_failure:
        # pipeline=None (phase1 経路 or 例外経路)。

        store = _RaisingStore(orch) if scenario == "start_run_failure" else orch

        rt = OrchestratorRuntime(
            config=OrchestratorConfig(),
            orch_store=store,
            context_builder=builder,
            pairs=list(pairs),
            quote_provider=quote_provider,
            detector=_StubDetector(pairs),
            pipeline=pipeline,
        )
        if notify_raises:
            def _boom(pair, result):
                raise RuntimeError("notify boom")
            rt._notify_planning_result = _boom  # type: ignore[method-assign]
        return rt

    return _factory


@pytest.mark.parametrize("scenario,expect_decision", [
    ("direct_hold", "direct_hold"),
    ("reject", "reject"),
    ("plan_create", "plan_create"),
    ("pipeline_failed", "failed"),
    ("phase1_observe", "direct_hold"),
    ("quote_failure", "error"),
    ("context_build_failure", "error"),
])
def test_start_result_one_to_one(caplog, planning_runtime_factory, scenario, expect_decision):
    rt = planning_runtime_factory(scenario)
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    starts = _count(caplog.records, "[ORCH] planning start")
    results = _count(caplog.records, "[ORCH] planning result")
    assert starts == 1
    assert results == 1
    msg = _decisions(caplog.records)[0]
    assert f"decision={expect_decision}" in msg
    # non-error decision は必ず reason= を伴う (判断理由を残す契約)。plan_create も
    # pipeline 側 fallback ("plan created") で非空 reason を保証するため例外を設けない。
    if expect_decision != "error":
        assert "reason=" in msg


def test_start_run_failure_emits_neither(caplog, planning_runtime_factory):
    """start_run 自体が落ちたら start も result も出ない (start=0/result=0)。

    さらに ef0ad88 の load-bearing な挙動を lock する:
    start_run が try 内で落ちても (a) cycle 全体は abort せず finally まで到達し、
    (b) detector.mark_attempted が呼ばれて debounce 窓が閉じる。snapshot 未到達なので
    committed は空 (baseline は未消費)。
    """
    rt = planning_runtime_factory("start_run_failure")
    detector = rt._detector
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    assert _count(caplog.records, "[ORCH] planning start") == 0
    assert _count(caplog.records, "[ORCH] planning result") == 0
    # start_run 失敗でも finally で mark_attempted が走り (debounce 窓が閉じる)、
    # committed は空 (snapshot 未到達で baseline 未消費)。cycle 全体は abort しない。
    assert detector.attempted == ["USDJPY=X"]
    assert detector.committed == []


def test_notify_failure_keeps_plan_create_result(caplog, planning_runtime_factory):
    """通知が例外を投げても run=ok・result=plan_create を維持する。"""
    rt = planning_runtime_factory("plan_create", notify_raises=True)
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    results = _decisions(caplog.records)
    assert len(results) == 1
    assert "decision=plan_create" in results[0]
    assert "decision=error" not in results[0]


def test_start_result_one_to_one_multi_pair(caplog, planning_runtime_factory):
    """複数 pair でも各 pair で start/result 各 1 件。"""
    rt = planning_runtime_factory("direct_hold", pairs=["USDJPY=X", "EURUSD=X"])
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    starts = [r.getMessage() for r in caplog.records if "[ORCH] planning start" in r.getMessage()]
    results = [r.getMessage() for r in caplog.records if "[ORCH] planning result" in r.getMessage()]
    assert len(starts) == 2 and len(results) == 2
    for pair in ("USDJPY=X", "EURUSD=X"):
        assert sum(pair in m for m in starts) == 1
        assert sum(pair in m for m in results) == 1
