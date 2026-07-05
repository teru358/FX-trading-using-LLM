"""shadow_metrics の trade_horizon フィルタのテスト (codex Low)。

daily summary は運用中の horizon (day か swing) 単体で見るべきだが、従来の
compute_shadow_metrics は全 plan/hindsight を horizon 区別なく合算していた
(swing 時代の gross PnL-R と day 時代の net PnL-R が混ざる等)。trade_horizon を
渡すと該当 horizon の plan/agent_runs/hindsight だけに絞り込めることを確認する。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.shadow_metrics import compute_shadow_metrics

NOW = datetime(2026, 7, 5, 12, 0, 0)


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def _plan_with_hindsight(
    store: OrchestratorStore, *, horizon: str, pnl_r: float,
) -> None:
    """horizon 指定の plan + agent_run + shadow_trigger + evaluated hindsight を 1 セット作る。"""
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X", trade_horizon=horizon)
    store.finish_run(run_id, status="ok")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon=horizon, direction="long",
        entry_conditions_json=[], action_json={"sl": 149.0, "tp": 152.0, "rr": 2.0},
        invalidation_json=[], expires_at=NOW + timedelta(days=1),
        created_by_run_id=run_id, status="triggered",
    )
    trigger_id = store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=NOW - timedelta(days=2), trigger_price=150.0,
        sl=149.0, tp=152.0, rr=2.0,
    )
    hs_id = store.record_hindsight_evaluation(shadow_trigger_id=trigger_id, horizon_seconds=86400)
    store.update_hindsight_evaluation(
        hs_id, status="evaluated", evaluated_at=NOW,
        mfe_r=abs(pnl_r), mae_r=-0.5, pnl_r=pnl_r, would_hit_sl=pnl_r < 0, would_hit_tp=pnl_r > 0,
    )


def test_trade_horizon_filter_counts_only_matching_horizon(store: OrchestratorStore) -> None:
    _plan_with_hindsight(store, horizon="swing", pnl_r=2.0)
    _plan_with_hindsight(store, horizon="day", pnl_r=-1.0)

    day_metrics = compute_shadow_metrics(store, now=NOW, trade_horizon="day")
    assert day_metrics.plans_created == 1
    assert day_metrics.plans_triggered == 1
    assert day_metrics.hindsight_evaluated == 1
    assert day_metrics.avg_pnl_r == pytest.approx(-1.0)
    assert day_metrics.agent_runs_total == 1

    swing_metrics = compute_shadow_metrics(store, now=NOW, trade_horizon="swing")
    assert swing_metrics.plans_created == 1
    assert swing_metrics.hindsight_evaluated == 1
    assert swing_metrics.avg_pnl_r == pytest.approx(2.0)


def test_trade_horizon_none_counts_both_horizons_backward_compat(
    store: OrchestratorStore,
) -> None:
    """trade_horizon 未指定 (None) は従来通り全 horizon 合算 (後方互換)。"""
    _plan_with_hindsight(store, horizon="swing", pnl_r=2.0)
    _plan_with_hindsight(store, horizon="day", pnl_r=-1.0)

    metrics = compute_shadow_metrics(store, now=NOW, trade_horizon=None)
    assert metrics.plans_created == 2
    assert metrics.hindsight_evaluated == 2
    assert metrics.avg_pnl_r == pytest.approx(0.5)
    assert metrics.agent_runs_total == 2

    # default 引数省略時も同じ (デフォルト None)
    default_metrics = compute_shadow_metrics(store, now=NOW)
    assert default_metrics.plans_created == 2
