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


# ── gate spec F-7: real/cf 分離 + label 集計 ──────────────────

from datetime import timedelta

from src.utils.clock import db_now


def _gate_plan(store, *, status="pending_approval") -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    return store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status=status,
    )


def test_real_plan_counts_exclude_cf_labels(tmp_path):
    """rejected/unanswered は実性能 plan_counts から除外。gate OFF (NULL) は含む
    (NOT IN の NULL 罠 — codex 2巡目 Medium の回帰テスト)。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    _gate_plan(store, status="active")           # gate OFF 相当 (gate_decision NULL)
    p_rej = _gate_plan(store)
    store.try_decide_gate(p_rej, "rejected")
    p_un = _gate_plan(store)
    store.try_close_pending_unanswered(p_un, "expired")
    p_appr = _gate_plan(store)
    store.try_decide_gate(p_appr, "approved")    # approved は real 側に含む

    raw = store.get_shadow_metrics_raw()
    # active 2 件 (NULL + approved)。rejected/expired(unanswered) は数えない
    assert raw["plan_counts"].get("active") == 2
    assert "rejected" not in raw["plan_counts"]
    assert raw["plan_counts"].get("expired") is None


def _cf_run(store) -> int:
    return store.start_run("OrchestratorRuntime", pair="USDJPY=X")


def test_cf_hindsight_excluded_from_real_aggregates(tmp_path):
    """cf 行の hindsight は実性能 avg_pnl_r に混入しない。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    p_rej = _gate_plan(store)
    store.try_decide_gate(p_rej, "rejected")
    trig_id = store.finalize_cf_trigger(
        p_rej, run_id=_cf_run(store), enqueue_hindsight=True, horizon_seconds=60,
        triggered_at=db_now() - timedelta(hours=2), trigger_price=150.0,
        spread_pips=1.0)
    ev = store.get_pending_hindsight_evaluations(now=db_now())[0]
    store.update_hindsight_evaluation(
        ev.id, status="evaluated", evaluated_at=db_now(), pnl_r=5.0)
    raw = store.get_shadow_metrics_raw()
    assert raw["avg_pnl_r"] is None              # real 側は空 (cf の 5.0 が混ざらない)
    assert raw["gate_avg_pnl_r"].get("rejected") == 5.0


def test_gate_label_counts(tmp_path):
    store = OrchestratorStore(tmp_path / "orch.db")
    p1 = _gate_plan(store); store.try_decide_gate(p1, "approved")
    p2 = _gate_plan(store); store.try_decide_gate(p2, "rejected")
    p3 = _gate_plan(store); store.try_close_pending_unanswered(p3, "expired")
    raw = store.get_shadow_metrics_raw()
    assert raw["gate_plan_counts"] == {"approved": 1, "rejected": 1, "unanswered": 1}


def test_gate_labels_respect_horizon(tmp_path):
    """daily summary の horizon 絞りが gate 行にも効く (swing 混入防止 —
    codex plan review Medium)。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    p_day = _gate_plan(store)
    store.try_decide_gate(p_day, "rejected")
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    p_swing = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status="pending_approval")
    store.try_decide_gate(p_swing, "rejected")
    raw = store.get_shadow_metrics_raw(trade_horizon="day")
    assert raw["gate_plan_counts"] == {"rejected": 1}    # swing は数えない


def test_compute_metrics_gate_labels_shape(tmp_path):
    from src.orchestrator.shadow_metrics import compute_shadow_metrics
    store = OrchestratorStore(tmp_path / "orch.db")
    p = _gate_plan(store); store.try_decide_gate(p, "rejected")
    store.finalize_cf_trigger(
        p, run_id=_cf_run(store), enqueue_hindsight=False, horizon_seconds=3600,
        triggered_at=db_now(), trigger_price=150.0, spread_pips=1.0)
    m = compute_shadow_metrics(store, now=db_now())
    g = m.gate_labels["rejected"]
    assert g["plans"] == 1
    assert g["triggers"] == 1
    assert g["trigger_rate"] == 1.0              # spec F-7 要求の構造化値
    assert "avg_pnl_r" in g


def test_superseded_pending_count(tmp_path):
    """superseded pending 件数は cf_state='superseded' マーカーで数える (F-7)。"""
    from src.orchestrator.shadow_metrics import compute_shadow_metrics
    store = OrchestratorStore(tmp_path / "orch.db")
    _gate_plan(store)                            # pending → supersede される
    store.supersede_active_plans("USDJPY=X")
    m = compute_shadow_metrics(store, now=db_now())
    assert m.gate_superseded_pending == 1
