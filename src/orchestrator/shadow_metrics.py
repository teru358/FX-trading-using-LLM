"""shadow 検証 metric の集計 (plan Phase 4 Task 4.3 / spec §8.2)。

OrchestratorStore.get_shadow_metrics_raw() の生カウントから rate / 平均を導出し
ShadowMetrics dataclass にまとめる。Phase 3 移行判断 (trigger rate + hindsight) の
主指標を 1 か所で提供する。SQL は store 側、shaping (rate/平均) はここ。
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    # 6. approval gate (spec F-7): label 別 {plans, triggers, trigger_rate, avg_pnl_r}。
    # approved=real 成績 / rejected・unanswered=反実仮想成績。空 dict = gate 未使用。
    gate_labels: dict = field(default_factory=dict)
    # superseded pending 件数 (cf_state='superseded' マーカー、放置率の解釈補助)
    gate_superseded_pending: int = 0
    # 承認遅延コスト: stamp 済み承認 plan の 実trigger − stamp の平均秒 (副産物)
    gate_approval_latency_avg_sec: float | None = None


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def compute_shadow_metrics(
    store: "OrchestratorStore", *, now: datetime, trade_horizon: str | None = None,
) -> ShadowMetrics:
    """store の生カウントから ShadowMetrics を組み立てる。

    now は将来の窓限定集計のために受けておく (現状は窓での絞り込みはしない)。
    `trade_horizon` を渡すと plan/hindsight/agent_runs を day | swing で絞り込む
    (daily summary で運用中の horizon だけを見せる, codex Low)。None なら従来通り
    全 horizon 合算 (後方互換)。
    """
    raw = store.get_shadow_metrics_raw(trade_horizon=trade_horizon)
    plan_counts: dict = raw["plan_counts"]
    run_counts: dict = raw["run_counts"]
    hs_counts: dict = raw["hindsight_counts"]

    plans_created = sum(plan_counts.values())
    plans_triggered = plan_counts.get("triggered", 0)

    agent_runs_total = sum(run_counts.values())
    agent_runs_failed = run_counts.get("failed", 0)

    hindsight_evaluated = hs_counts.get("evaluated", 0)

    gate_labels = {}
    for label, count in raw["gate_plan_counts"].items():
        triggers = raw["gate_trigger_counts"].get(label, 0)
        gate_labels[label] = {
            "plans": count,
            "triggers": triggers,
            "trigger_rate": _rate(triggers, count),
            "avg_pnl_r": raw["gate_avg_pnl_r"].get(label),
        }

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
        # 注: spread_cost_r (net PnL-R) は day horizon 導入 (2026-07-05) と同時期に
        # 追加されたため、trade_horizon="day" で絞り込めば net のみになり gross との
        # 混在はほぼ解消する。trade_horizon=None (全期間合算) の場合は、day 導入以前の
        # swing 行 (gross) と day 行 (net) が依然として混在しうる点に注意。
        avg_pnl_r=raw["avg_pnl_r"],
        sl_hit_rate=_rate(raw["sl_hits"], hindsight_evaluated),
        tp_hit_rate=_rate(raw["tp_hits"], hindsight_evaluated),
        agent_runs_total=agent_runs_total,
        agent_runs_failed=agent_runs_failed,
        llm_failure_rate=_rate(agent_runs_failed, agent_runs_total),
        freshness_blocks=raw["freshness_blocks"],
        gate_labels=gate_labels,
        gate_superseded_pending=raw["gate_superseded_pending"],
        gate_approval_latency_avg_sec=raw["gate_approval_latency_avg_sec"],
    )
