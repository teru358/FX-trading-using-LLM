"""PlannerAgent (Task 2.5) — 機会判定 + 最終承認。

design §5.2 Step2 (opportunity scan) と Step4 (final decision)。同一 snapshot に対し
2 回 dispatch される (機会判定 → ExecutionOpinionAgent 起案 → 最終承認)。

- scan_opportunity: context から「今 planning する価値があるか」を yes/no + direction
  + score/confidence で返す (PlannerOpportunity)。
- final_decision: ExecutionOpinionAgent の draft を見て accept / revise / reject を返す
  (PlannerFinalDecision)。**これが最終判断** (vote の weighted sum ではない、§13#6)。

LLM raw text を直接使わず schema で厳格 parse。parse 失敗は SchemaParseError。
"""
from __future__ import annotations

import json
from typing import Any

from src.orchestrator.execution_opinion_agent import (
    _horizon_guidance,
    _position_guidance,
)
from src.orchestrator.schemas import (
    ExecutionPlanDraft,
    PlannerFinalDecision,
    PlannerOpportunity,
)

_SCAN_SYSTEM = (
    "You are an FX planning supervisor. Decide whether there is a tradeable "
    "opportunity right now. Return STRICT JSON only (no prose, no markdown fences):\n"
    '{"opportunity": "yes"|"no", "direction": "long"|"short"|"none", '
    '"score": number in [0,1], "confidence": number in [0,1], '
    '"reasoning_summary": string, "missing_inputs": [string]}'
)

_FINAL_SYSTEM = (
    "You are an FX planning supervisor making the FINAL decision on a proposed "
    "trade plan draft. You have hard authority. Return STRICT JSON only:\n"
    '{"decision": "accept"|"revise"|"reject", "final_score": number in [0,1] or null, '
    '"confidence": number in [0,1] or null, "reasoning_summary": string, '
    '"revision_request": object or null}\n'
    'If decision is "revise", revision_request MUST be a non-null object describing '
    "the change."
)


class PlannerAgent:
    def __init__(self, agent_llm) -> None:
        # agent_llm: src.config.schema.AgentLlm (client + 解決済 temperature)
        self._llm = agent_llm.client
        self._temperature = agent_llm.temperature

    async def scan_opportunity(
        self, *, pair: str, context: dict[str, Any], temperature: float | None = None
    ) -> PlannerOpportunity:
        temp = self._temperature if temperature is None else temperature
        user = "\n".join(
            part
            for part in [
                f"pair: {pair}",
                _horizon_guidance(context),
                _position_guidance(context),
                "decision_context:",
                json.dumps(_compact_context(context), ensure_ascii=False),
                "Decide if there is a tradeable opportunity. Return STRICT JSON.",
            ]
            if part
        )
        raw = await self._llm.chat(
            [{"role": "system", "content": _SCAN_SYSTEM}, {"role": "user", "content": user}],
            temperature=temp,
        )
        return PlannerOpportunity.from_llm_json(raw)

    async def final_decision(
        self,
        *,
        pair: str,
        context: dict[str, Any],
        draft: ExecutionPlanDraft,
        temperature: float | None = None,
    ) -> PlannerFinalDecision:
        temp = self._temperature if temperature is None else temperature
        user = "\n".join(
            part
            for part in [
                f"pair: {pair}",
                _horizon_guidance(context),
                _position_guidance(context),
                "decision_context:",
                json.dumps(_compact_context(context), ensure_ascii=False),
                "proposed_draft:",
                json.dumps(_draft_summary(draft), ensure_ascii=False),
                "Make the final decision (accept/revise/reject). Return STRICT JSON.",
            ]
            if part
        )
        raw = await self._llm.chat(
            [{"role": "system", "content": _FINAL_SYSTEM}, {"role": "user", "content": user}],
            temperature=temp,
        )
        return PlannerFinalDecision.from_llm_json(raw)


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    # Codex Medium#2: 再エントリー抑制・直近損益反映のため recent_exits /
    # recent_trade_stats / similar_cases も渡す。
    return {
        "quote": context.get("quote"),
        "position": context.get("position"),
        "current_plan": context.get("current_plan"),
        "technical": context.get("technical"),
        "news": context.get("news"),
        "policy": context.get("policy"),
        "move_maturity": context.get("move_maturity"),
        "recent_decisions": context.get("recent_decisions"),
        "recent_exits": context.get("recent_exits"),
        "recent_trade_stats": context.get("recent_trade_stats"),
        "similar_cases": context.get("similar_cases"),
    }


def _draft_summary(draft: ExecutionPlanDraft) -> dict[str, Any]:
    """final_decision プロンプト用に draft を要約。expires_at は ISO 文字列化。"""
    return {
        "direction": draft.direction,
        "entry_conditions": [c.to_dict() for c in draft.entry_conditions],
        "action": draft.action,
        "invalidation": [c.to_dict() for c in draft.invalidation],
        "expires_at": draft.expires_at.isoformat(),
        "reasoning_summary": draft.reasoning_summary,
    }
