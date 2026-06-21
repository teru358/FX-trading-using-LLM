"""shadow_metrics の集計テスト (plan Phase 4 Task 4.3 / spec §8.2)。

Phase 3 移行判断のための metric を OrchestratorStore から集計する:
1. plan lifecycle count (created/triggered/invalidated/expired/superseded)
2. trigger rate (= triggered / created)
3. hindsight 集計 (avg MFE-R/MAE-R/PnL-R, SL/TP 到達率, pending/evaluated 数)
4. LLM failure rate (failed agent_runs / total)
5. freshness block count (data_freshness_snapshots の issues 件数)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.shadow_metrics import ShadowMetrics, compute_shadow_metrics

NOW = datetime(2026, 6, 22, 12, 0, 0)


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def _plan(store: OrchestratorStore, *, status: str = "active") -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    return store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={"sl": 149.0, "tp": 152.0, "rr": 2.0},
        invalidation_json=[], expires_at=NOW + timedelta(days=1),
        created_by_run_id=run_id, status=status,
    )


def _trigger(store: OrchestratorStore, plan_id: int) -> int:
    return store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=NOW - timedelta(days=2), trigger_price=150.0,
        sl=149.0, tp=152.0, rr=2.0,
    )


def test_lifecycle_counts(store: OrchestratorStore) -> None:
    _plan(store, status="active")
    _plan(store, status="triggered")
    _plan(store, status="invalidated")
    _plan(store, status="expired")
    _plan(store, status="superseded")
    m = compute_shadow_metrics(store, now=NOW)
    assert isinstance(m, ShadowMetrics)
    assert m.plans_created == 5
    assert m.plans_triggered == 1
    assert m.plans_invalidated == 1
    assert m.plans_expired == 1
    assert m.plans_superseded == 1


def test_trigger_rate(store: OrchestratorStore) -> None:
    """trigger rate = triggered / created。"""
    for _ in range(3):
        _plan(store, status="active")
    _plan(store, status="triggered")
    m = compute_shadow_metrics(store, now=NOW)
    assert m.plans_created == 4
    assert m.trigger_rate == pytest.approx(0.25)


def test_trigger_rate_zero_when_no_plans(store: OrchestratorStore) -> None:
    m = compute_shadow_metrics(store, now=NOW)
    assert m.plans_created == 0
    assert m.trigger_rate == 0.0


def test_hindsight_aggregation(store: OrchestratorStore) -> None:
    """evaluated hindsight 行の MFE-R/MAE-R/PnL-R 平均と SL/TP 到達率を集計。"""
    p1 = _plan(store, status="triggered")
    t1 = _trigger(store, p1)
    h1 = store.record_hindsight_evaluation(shadow_trigger_id=t1, horizon_seconds=86400)
    store.update_hindsight_evaluation(
        h1, status="evaluated", evaluated_at=NOW,
        mfe_r=2.0, mae_r=-0.5, pnl_r=2.0, would_hit_sl=False, would_hit_tp=True,
    )
    p2 = _plan(store, status="triggered")
    t2 = _trigger(store, p2)
    h2 = store.record_hindsight_evaluation(shadow_trigger_id=t2, horizon_seconds=86400)
    store.update_hindsight_evaluation(
        h2, status="evaluated", evaluated_at=NOW,
        mfe_r=1.0, mae_r=-1.0, pnl_r=-1.0, would_hit_sl=True, would_hit_tp=False,
    )
    # 1 件は pending (集計の母数に入れない)
    p3 = _plan(store, status="triggered")
    t3 = _trigger(store, p3)
    store.record_hindsight_evaluation(shadow_trigger_id=t3, horizon_seconds=86400)

    m = compute_shadow_metrics(store, now=NOW)
    assert m.hindsight_evaluated == 2
    assert m.hindsight_pending == 1
    assert m.avg_mfe_r == pytest.approx(1.5)
    assert m.avg_mae_r == pytest.approx(-0.75)
    assert m.avg_pnl_r == pytest.approx(0.5)
    assert m.sl_hit_rate == pytest.approx(0.5)
    assert m.tp_hit_rate == pytest.approx(0.5)


def test_llm_failure_rate(store: OrchestratorStore) -> None:
    """failed agent_runs / total agent_runs。"""
    ok = store.start_run("PlannerAgent", pair="USDJPY=X")
    store.finish_run(ok, status="ok")
    bad = store.start_run("PlannerAgent", pair="USDJPY=X")
    store.finish_run(bad, status="failed", error_type="Timeout")
    m = compute_shadow_metrics(store, now=NOW)
    assert m.agent_runs_total == 2
    assert m.agent_runs_failed == 1
    assert m.llm_failure_rate == pytest.approx(0.5)


def test_freshness_block_count(store: OrchestratorStore) -> None:
    """issues 非空の freshness 行数を freshness block として数える。"""
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    store.record_freshness(snapshot_id=snap, pair="USDJPY=X", issues=["spread_too_wide"])
    store.record_freshness(snapshot_id=snap, pair="USDJPY=X", issues=[])  # block でない
    m = compute_shadow_metrics(store, now=NOW)
    assert m.freshness_blocks == 1
