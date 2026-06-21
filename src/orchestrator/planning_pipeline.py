"""planning_pipeline (Task 2.6-2.9) — Layer2 の判断パイプライン。

design §5.2 (Step2-6) を直列駆動する。最終判断は PlannerAgent.final_decision
(§13#6: vote の weighted sum ではない)。RiskGateWorker は hard veto (§13#7)。

Flow (opportunity=yes の場合):
  Step2 PlannerAgent.scan_opportunity → agent_outputs 保存
  Step3 ExecutionOpinionAgent.draft   → agent_outputs + execution_opinions 保存
  Step4 PlannerAgent.final_decision   → accept/revise/reject
  Step5 RiskGateWorker.pre_check      → pass / reject(structural|fixable)
  Step6 create_trade_plan → supersede_active_plans → record_decision(plan_create)
        → record_vote (どの agent output が反映されたか紐付け)

re-draft (§5.3, Task 2.7): fixable reject は ExecutionOpinion 1 回だけ再起案 → 再照査。
  2 回目も reject なら record_decision(reject)。structural reject は再起案しない。

fail-safe (§5.4, Task 2.8): SchemaParseError / CircuitOpenError / TimeoutError は
  新規 plan を作らず PipelineResult(outcome='failed') を返す。run の failed 記録は
  呼び出し側 (runtime) が finish_run(status='failed') で行う。

record 順序 (§5.2): 中間 opinion を agent_outputs/execution_opinions に保存 →
  Step4 final decision を record_decision → decision_id 取得後 record_vote で紐付け。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.config.schema import OrchestratorConfig
from src.data.orchestrator_store import OrchestratorStore
from src.llm.client import CircuitOpenError
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
from src.orchestrator.planner_agent import PlannerAgent
from src.orchestrator.risk_gate import RiskGateWorker
from src.orchestrator.schemas import (
    ExecutionPlanDraft,
    PlannerFinalDecision,
    SchemaParseError,
)

logger = logging.getLogger(__name__)

# fail-safe で握る想定内の例外 (新規 plan を作らず failed に倒す)。
# Python 3.11+ では asyncio.TimeoutError is TimeoutError なので TimeoutError のみで足りる。
_FAILSAFE_EXC = (SchemaParseError, CircuitOpenError, TimeoutError)


@dataclass
class PipelineResult:
    outcome: str  # "direct_hold" | "plan_create" | "reject" | "failed"
    plan_id: int | None = None
    decision_ids: list[int] = field(default_factory=list)
    redraft_count: int = 0
    error: str | None = None
    # ── shadow 通知用 (Phase 5)。記録系は store が source of truth だが、通知に必要な
    # 表示値 (direction/score/confidence/reason/supersede) を runtime に再 query させず
    # ここで運ぶ。outcome が plan_create/reject のときのみ意味を持つ。
    direction: str | None = None
    score: float | None = None
    confidence: float | None = None
    reason: str | None = None
    superseded_plan_ids: list[int] = field(default_factory=list)


class PlanningPipeline:
    def __init__(
        self,
        *,
        orch_store: OrchestratorStore,
        planner: PlannerAgent,
        execution_agent: ExecutionOpinionAgent,
        risk_gate: RiskGateWorker,
        config: OrchestratorConfig,
    ) -> None:
        self._orch = orch_store
        self._planner = planner
        self._exec = execution_agent
        self._risk = risk_gate
        self._config = config

    async def run(self, *, pair: str, context: dict[str, Any], run_id: int) -> PipelineResult:
        """1 snapshot 分の planning を駆動する。

        パイプラインは LLM 呼び出しを await するため async。同期コンテキスト
        (runtime の planning thread) からは ``asyncio.run(pipeline.run(...))`` で
        呼ぶ (Task 2.11)。そこでは event loop が無いので二重 loop にならない。
        """
        snapshot_id = context["snapshot_id"]
        horizon = self._config.policy.trade_horizon
        try:
            return await self._pipeline(pair, context, run_id, snapshot_id, horizon)
        except _FAILSAFE_EXC as exc:
            # 想定内の fail-safe (parse/timeout/circuit)。warning で十分。
            logger.warning(
                "[ORCH] planning fail-safe for %s: %s: %s", pair, type(exc).__name__, exc
            )
            return PipelineResult(outcome="failed", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # 想定外 (store DB エラー等)。§5.4「planning thread は死なせない」。
            # plan を作っても decision を残せなかった等の不整合は orphan を残しうるが、
            # _commit_plan が record 順序 (create→decision→vote→supersede) でこれを
            # 最小化する。ここはあくまで最後の安全網。
            logger.exception("[ORCH] planning unexpected error for %s — failing safe", pair)
            return PipelineResult(outcome="failed", error=f"{type(exc).__name__}: {exc}")

    async def _pipeline(
        self, pair: str, context: dict[str, Any], run_id: int, snapshot_id: int, horizon: str
    ) -> PipelineResult:
        # ── Step 2: opportunity scan ──────────────────────────
        opp = await self._planner.scan_opportunity(pair=pair, context=context)
        self._orch.record_agent_output(
            run_id=run_id, agent_name="PlannerAgent", pair=pair,
            output_type="opportunity", action=opp.direction, score=opp.score,
            confidence=opp.confidence, reasoning_summary=opp.reasoning_summary,
            structured_payload={
                "opportunity": opp.opportunity, "direction": opp.direction,
                "missing_inputs": opp.missing_inputs,
            },
        )

        if opp.opportunity == "no":
            did = self._orch.record_decision(
                run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                decision_type="direct_hold", decision="hold",
                final_score=opp.score, confidence=opp.confidence,
                reasoning_summary=opp.reasoning_summary, trade_horizon=horizon,
            )
            return PipelineResult(outcome="direct_hold", decision_ids=[did])

        # ── Step 3-5: draft → final → risk (再起案ループ) ──────
        # opportunity=yes なのに direction が long/short でない = 矛盾出力。
        # 黙って long に倒さず fail-safe (no plan + failed) に倒す (MEDIUM-1)。
        if opp.direction not in ("long", "short"):
            raise SchemaParseError(
                f"opportunity=yes but direction={opp.direction!r} (expected long/short)"
            )
        direction = opp.direction
        feedback: list[str] | None = None
        redraft_count = 0
        max_redraft = 1  # 修正可能 reject / revise は 1 回だけ再起案 (§5.3)

        while True:
            draft = await self._exec.draft(
                pair=pair, direction=direction, context=context,
                revision_feedback=feedback,
            )
            self._persist_opinion(run_id, pair, draft)

            final = await self._planner.final_decision(
                pair=pair, context=context, draft=draft
            )

            # PlannerAgent が reject → そこで終了 (risk gate 前)。
            if final.decision == "reject":
                did = self._record_reject(
                    run_id, snapshot_id, pair, horizon, final, risk_result=None
                )
                return PipelineResult(
                    outcome="reject", decision_ids=[did], redraft_count=redraft_count,
                    reason=f"planner reject: {final.reasoning_summary}",
                )

            # PlannerAgent が revise → draft を採用せず 1 回だけ再起案 (§13#6: 最終権限は
            # planner)。予算切れなら reject 終了。**revise を accept 扱いしない** (CRITICAL-1)。
            if final.decision == "revise":
                if redraft_count < max_redraft:
                    redraft_count += 1
                    feedback = _revise_feedback(final)
                    continue
                did = self._record_reject(
                    run_id, snapshot_id, pair, horizon, final, risk_result=None
                )
                return PipelineResult(
                    outcome="reject", decision_ids=[did], redraft_count=redraft_count,
                    reason=f"planner revise exhausted: {final.reasoning_summary}",
                )

            # accept のみ risk gate (hard veto) へ。
            risk = self._risk.pre_check(draft, context)
            if risk.passed:
                return self._commit_plan(
                    run_id, snapshot_id, pair, horizon, draft, final, risk, redraft_count
                )

            # risk reject。structural は再起案しない。fixable は 1 回だけ。
            if risk.reject_class == "fixable" and redraft_count < max_redraft:
                redraft_count += 1
                feedback = list(risk.issues)
                continue

            did = self._record_reject(
                run_id, snapshot_id, pair, horizon, final, risk_result=risk
            )
            risk_reason = "; ".join(risk.issues) if risk.issues else "structural"
            return PipelineResult(
                outcome="reject", decision_ids=[did], redraft_count=redraft_count,
                reason=f"risk reject ({risk.reject_class}): {risk_reason}",
            )

    # ── persistence helpers ──────────────────────────────────

    def _persist_opinion(self, run_id: int, pair: str, draft: ExecutionPlanDraft) -> None:
        action = draft.action
        self._orch.record_agent_output(
            run_id=run_id, agent_name="ExecutionOpinionAgent", pair=pair,
            output_type="execution_draft",
            action="buy" if draft.direction == "long" else "sell",
            reasoning_summary=draft.reasoning_summary,
            structured_payload=draft.to_storage_dict(),
        )
        self._orch.record_execution_opinion(
            run_id=run_id, pair=pair,
            action="buy" if draft.direction == "long" else "sell",
            sl=action.get("sl"), tp=action.get("tp"), rr=action.get("rr"),
            reasoning_summary=draft.reasoning_summary,
        )

    def _record_reject(
        self, run_id, snapshot_id, pair, horizon,
        final: PlannerFinalDecision, risk_result,
    ) -> int:
        return self._orch.record_decision(
            run_id=run_id, snapshot_id=snapshot_id, pair=pair,
            decision_type="reject", decision="reject",
            final_score=final.final_score, confidence=final.confidence,
            reasoning_summary=final.reasoning_summary, trade_horizon=horizon,
            risk_gate_result=risk_result.to_dict() if risk_result is not None else None,
        )

    def _commit_plan(
        self, run_id, snapshot_id, pair, horizon,
        draft: ExecutionPlanDraft, final: PlannerFinalDecision, risk, redraft_count,
    ) -> PipelineResult:
        storage = draft.to_storage_dict()
        side = "buy" if draft.direction == "long" else "sell"
        # orphan 防止 (Codex High#2): まず非 active (requires_replan) で作る。
        # get_active_plans は active のみ拾うので、decision/vote 記録前にクラッシュしても
        # この plan は active として可視化されない。最後に update_plan_status('active')。
        plan_id = self._orch.create_trade_plan(
            pair=pair, snapshot_id=snapshot_id, horizon=horizon,
            direction=draft.direction,
            entry_conditions_json=storage["entry_conditions"],
            action_json=storage["action"],
            invalidation_json=storage["invalidation"],
            expires_at=draft.expires_at, created_by_run_id=run_id,
            status="requires_replan",
        )
        # write 順序: create(pending) → decision → vote → supersede → activate。
        did = self._orch.record_decision(
            run_id=run_id, snapshot_id=snapshot_id, pair=pair,
            decision_type="plan_create", decision=side,
            plan_id=plan_id, final_score=final.final_score, confidence=final.confidence,
            reasoning_summary=final.reasoning_summary, trade_horizon=horizon,
            risk_gate_result=risk.to_dict(),
        )
        # decision_id 取得後に vote を紐付け (§5.2)。
        self._orch.record_vote(
            decision_id=did, agent_run_id=run_id, agent_name="ExecutionOpinionAgent",
            vote_action=side, vote_score=final.final_score,
            vote_confidence=final.confidence, reflected_in_plan=True,
        )
        # active plan policy: pair 単位最大 1 (§6.1)。旧 active を superseded に
        # (新 plan はまだ requires_replan なので except 不要だが、明示で安全側)。
        superseded = self._orch.supersede_active_plans(pair, except_plan_id=plan_id)
        # 全 write 成功 → ここで初めて active 化 (orphan window を閉じる)。
        self._orch.update_plan_status(plan_id, "active")
        return PipelineResult(
            outcome="plan_create", plan_id=plan_id, decision_ids=[did],
            redraft_count=redraft_count,
            direction=draft.direction, score=final.final_score,
            confidence=final.confidence, reason=final.reasoning_summary,
            superseded_plan_ids=superseded,
        )


def _revise_feedback(final: PlannerFinalDecision) -> list[str]:
    """planner の revise 指示を再起案プロンプト用 feedback に整形する。"""
    lines = [f"planner requested revision: {final.reasoning_summary}"]
    if final.revision_request:
        lines.append(f"revision_request: {final.revision_request}")
    return lines
