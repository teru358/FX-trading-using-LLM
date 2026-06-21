"""shadow 検証 metric の集計 (plan Phase 4 Task 4.3 / spec §8.2)。

OrchestratorStore.get_shadow_metrics_raw() の生カウントから rate / 平均を導出し
ShadowMetrics dataclass にまとめる。Phase 3 移行判断 (trigger rate + hindsight) の
主指標を 1 か所で提供する。SQL は store 側、shaping (rate/平均) はここ。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.orchestrator_store import OrchestratorStore


@dataclass
class ShadowMetrics:
    """Phase 3 移行判断のための shadow 検証 metric (spec §8.2)。"""
    # 1. plan lifecycle
    plans_created: int = 0
    plans_triggered: int = 0
    plans_invalidated: int = 0
    plans_expired: int = 0
    plans_superseded: int = 0
    # 2. trigger rate
    trigger_rate: float = 0.0
    # 3. hindsight
    hindsight_pending: int = 0
    hindsight_evaluated: int = 0
    hindsight_failed: int = 0
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    avg_pnl_r: float | None = None
    sl_hit_rate: float = 0.0
    tp_hit_rate: float = 0.0
    # 4. LLM failure
    agent_runs_total: int = 0
    agent_runs_failed: int = 0
    llm_failure_rate: float = 0.0
    # 5. freshness block
    freshness_blocks: int = 0


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def compute_shadow_metrics(
    store: "OrchestratorStore", *, now: datetime
) -> ShadowMetrics:
    """store の生カウントから ShadowMetrics を組み立てる。

    now は将来の窓限定集計のために受けておく (現状は全期間集計)。
    """
    raw = store.get_shadow_metrics_raw()
    plan_counts: dict = raw["plan_counts"]
    run_counts: dict = raw["run_counts"]
    hs_counts: dict = raw["hindsight_counts"]

    plans_created = sum(plan_counts.values())
    plans_triggered = plan_counts.get("triggered", 0)

    agent_runs_total = sum(run_counts.values())
    agent_runs_failed = run_counts.get("failed", 0)

    hindsight_evaluated = hs_counts.get("evaluated", 0)

    return ShadowMetrics(
        plans_created=plans_created,
        plans_triggered=plans_triggered,
        plans_invalidated=plan_counts.get("invalidated", 0),
        plans_expired=plan_counts.get("expired", 0),
        plans_superseded=plan_counts.get("superseded", 0),
        trigger_rate=_rate(plans_triggered, plans_created),
        hindsight_pending=hs_counts.get("pending", 0),
        hindsight_evaluated=hindsight_evaluated,
        hindsight_failed=hs_counts.get("failed", 0),
        avg_mfe_r=raw["avg_mfe_r"],
        avg_mae_r=raw["avg_mae_r"],
        avg_pnl_r=raw["avg_pnl_r"],
        sl_hit_rate=_rate(raw["sl_hits"], hindsight_evaluated),
        tp_hit_rate=_rate(raw["tp_hits"], hindsight_evaluated),
        agent_runs_total=agent_runs_total,
        agent_runs_failed=agent_runs_failed,
        llm_failure_rate=_rate(agent_runs_failed, agent_runs_total),
        freshness_blocks=raw["freshness_blocks"],
    )
