"""ExecutionOpinionAgent (Task 2.4) — LLM で ExecutionPlanDraft を起案する。

design §5.2 Step3。注入された LLMClient (role=price_analysis 相当) に decision
context を渡し、entry_conditions / action(SL/TP/RR) / invalidation を含む draft を
JSON で生成させる。出力は schemas.ExecutionPlanDraft.from_llm_json で厳格 parse。

LLM raw text を直接 plan にしない (§13#5)。parse 失敗は SchemaParseError として
上位 (planning_pipeline) に伝播し、fail-safe で no plan + failed run に倒す。

Task 2.7 re-draft: revision_feedback (risk gate の fixable issues や
revision_request) をプロンプトに添えて 1 回だけ再起案させる。
"""
from __future__ import annotations

import json
from typing import Any

from src.orchestrator.schemas import ExecutionPlanDraft

_SYSTEM = (
    "You are an FX execution planner. Given a decision context, produce a single "
    "trade plan draft as STRICT JSON only (no prose, no markdown fences).\n"
    "Schema: {\n"
    '  "direction": "long"|"short",\n'
    '  "entry_conditions": [{"type": "price_at_or_below"|"price_at_or_above"|'
    '"breakout_above"|"breakout_below", "value": number} | '
    '{"type": "spread_below", "value_pips": number} | '
    '{"type": "technical_status_is", "status": "ok"}],\n'
    '  "action": {"sl": number, "tp": number, "size_policy": string, "rr": number, "comment": string},\n'
    '  "invalidation": [{"type": "price_below"|"price_above", "value": number} | '
    '{"type": "technical_stale"|"news_conflict"|"expired"}],\n'
    '  "expires_at": ISO-8601 datetime,\n'
    '  "reasoning_summary": string,\n'
    '  "scale_in": boolean (true ONLY when adding to an existing same-direction position),\n'
    '  "new_signal_evidence": string or null (required when scale_in=true: the NEW '
    "signal justifying the add)\n"
    "}\n"
    "Use only the listed condition vocabularies. `and` semantics only."
)


class ExecutionOpinionAgent:
    def __init__(self, agent_llm) -> None:
        # agent_llm: src.config.schema.AgentLlm (client + 解決済 temperature)
        self._llm = agent_llm.client
        self._temperature = agent_llm.temperature

    async def draft(
        self,
        *,
        pair: str,
        direction: str,
        context: dict[str, Any],
        revision_feedback: list[str] | None = None,
        temperature: float | None = None,
    ) -> ExecutionPlanDraft:
        """draft を 1 件起案する。parse 失敗時 SchemaParseError。"""
        temp = self._temperature if temperature is None else temperature
        user = self._build_user_prompt(pair, direction, context, revision_feedback)
        raw = await self._llm.chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=temp,
        )
        return ExecutionPlanDraft.from_llm_json(raw)

    def _build_user_prompt(
        self,
        pair: str,
        direction: str,
        context: dict[str, Any],
        revision_feedback: list[str] | None,
    ) -> str:
        lines = [
            f"pair: {pair}",
            f"intended direction: {direction}",
            _horizon_guidance(context),
            _position_guidance(context),
            "decision_context:",
            json.dumps(_compact_context(context), ensure_ascii=False),
        ]
        if revision_feedback:
            lines.append(
                "PREVIOUS DRAFT WAS REJECTED. Fix these issues and re-draft:"
            )
            lines.extend(f"  - {issue}" for issue in revision_feedback)
        lines.append("Return the trade plan draft as STRICT JSON.")
        return "\n".join(line for line in lines if line)


def _horizon_guidance(context: dict[str, Any]) -> str:
    """context.policy から horizon 別の運用指針文を組む (spec 2026-07-05 S-2)。"""
    policy = context.get("policy") or {}
    horizon = policy.get("trade_horizon", "swing")
    if horizon == "day":
        ttl = policy.get("plan_ttl_max_hours") or 0
        ttl_line = (
            f" expires_at must be within {ttl} hours from now"
            " (the system clamps any longer value)." if ttl else ""
        )
        return (
            "Operating horizon: DAY trade. The plan must complete within hours,"
            " never overnight." + ttl_line +
            " Entry conditions must be reachable from the current price"
            " (guideline: 0.3-1.5x the 1h ATR). Keep RR >= 2."
        )
    return (
        "Operating horizon: SWING trade. Plans may span days."
        " Prefer pullback/retest conditional entries over chasing extended moves."
    )


def _position_guidance(context: dict[str, Any]) -> str:
    """建玉・既存 plan がある場合の指針文 (spec P-3)。無ければ空文字。"""
    parts: list[str] = []
    position = context.get("position") or {}
    if position.get("items"):
        parts.append(
            "An open position exists for this pair (decision_context.position)."
            " A same-direction plan is a scale-in: set scale_in=true and describe in"
            " new_signal_evidence a NEW signal that did not exist at the original"
            " entry (re-stating the original entry reason is not valid)."
            " An opposite-direction plan is a reversal: justify why the existing"
            " position's thesis is failing."
        )
    if context.get("current_plan"):
        parts.append(
            "An existing plan is already waiting for entry"
            " (decision_context.current_plan). If its premise still holds, prefer"
            " keeping it (direct_hold) over replacing it; a replacement must cite"
            " what changed."
        )
    return " ".join(parts)


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    """プロンプトに載せる主要フィールドだけ抜き出す (token 節約)。

    Codex Medium#2: 再エントリー抑制・直近損益反映のため recent_exits /
    recent_trade_stats / similar_cases / position も渡す。
    """
    return {
        "quote": context.get("quote"),
        "position": context.get("position"),
        "current_plan": context.get("current_plan"),
        "technical": context.get("technical"),
        "news": context.get("news"),
        "policy": context.get("policy"),
        "move_maturity": context.get("move_maturity"),
        "recent_exits": context.get("recent_exits"),
        "recent_trade_stats": context.get("recent_trade_stats"),
        "similar_cases": context.get("similar_cases"),
    }
