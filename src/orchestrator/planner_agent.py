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
import logging
from pathlib import Path
from typing import Any

from src.analysis.price_analyzer import load_user_notes
from src.orchestrator.execution_opinion_agent import (
    _horizon_guidance,
    _position_guidance,
)
from src.orchestrator.schemas import (
    ExecutionPlanDraft,
    PlannerFinalDecision,
    PlannerOpportunity,
)

logger = logging.getLogger(__name__)

# プロンプト総予算 (spec §2.D): ctx4096 から出力予約 ~512 tok と余裕を引いた
# 入力上限の目安 (~3k tok ≒ 12,000 chars)。ctx 拡張 (将来) 時はこの定数を上げる。
_PROMPT_BUDGET_CHARS = 12_000
_USER_NOTES_MAX_CHARS = 1_500

_ADVISORY_RULE = (
    "User notes, if present, are ADVISORY only: when they conflict with market "
    "data or technical signals, prioritize the data. Mention in reasoning_summary "
    "whether you followed or overrode any user note."
)

_SCAN_SYSTEM = (
    "You are an FX planning supervisor. Decide whether there is a tradeable "
    "opportunity right now. Return STRICT JSON only (no prose, no markdown fences):\n"
    '{"opportunity": "yes"|"no", "direction": "long"|"short"|"none", '
    '"score": number in [0,1], "confidence": number in [0,1], '
    '"reasoning_summary": string, "missing_inputs": [string]}\n' + _ADVISORY_RULE
)

_FINAL_SYSTEM = (
    "You are an FX planning supervisor making the FINAL decision on a proposed "
    "trade plan draft. You have hard authority. Return STRICT JSON only:\n"
    '{"decision": "accept"|"revise"|"reject", "final_score": number in [0,1] or null, '
    '"confidence": number in [0,1] or null, "reasoning_summary": string, '
    '"revision_request": object or null}\n'
    'If decision is "revise", revision_request MUST be a non-null object describing '
    "the change.\n" + _ADVISORY_RULE
)


class PlannerAgent:
    _NOTES_HEADER = "User notes (advisory, data takes priority):\n"

    def __init__(self, agent_llm, user_notes_path: "Path | None" = None) -> None:
        # agent_llm: src.config.schema.AgentLlm (client + 解決済 temperature)
        self._llm = agent_llm.client
        self._temperature = agent_llm.temperature
        self._user_notes_path = user_notes_path

    def _plan_notes_block(self, system: str, user_without_notes: str) -> str | None:
        """user_notes の ## plan セクションを advisory ブロックとして返す (毎回再読込)。

        総予算契約 (spec §2.D): notes は最低優先。notes 抜きプロンプト
        (system + user) の残余予算内でのみ注入し、残余ゼロ以下なら落とす。
        remaining はヘッダ (_NOTES_HEADER) + 最終 join 改行1つ分の overhead も
        差し引く — notes_block 全体が予算内に収まるようにするため。
        """
        if self._user_notes_path is None:
            return None
        notes = load_user_notes(self._user_notes_path, "plan")
        if not notes:
            return None
        # notes_block 全体 (ヘッダ + notes) + join 改行1つ分を予算から引く
        overhead = len(self._NOTES_HEADER) + 1
        remaining = (
            _PROMPT_BUDGET_CHARS - len(system) - len(user_without_notes) - overhead
        )
        cap = min(_USER_NOTES_MAX_CHARS, remaining)
        if cap <= 0:
            logger.warning(
                "[PLANNER] user plan notes dropped: prompt budget exhausted "
                "(remaining=%d)", remaining,
            )
            return None
        if len(notes) > cap:
            logger.warning(
                "[PLANNER] user plan notes truncated (%d > %d chars)", len(notes), cap,
            )
            notes = notes[:cap]
        return f"{self._NOTES_HEADER}{notes}"

    async def scan_opportunity(
        self, *, pair: str, context: dict[str, Any], temperature: float | None = None
    ) -> PlannerOpportunity:
        temp = self._temperature if temperature is None else temperature
        base_parts = [
            f"pair: {pair}",
            _horizon_guidance(context),
            _position_guidance(context),
            "decision_context:",
            json.dumps(_compact_context(context), ensure_ascii=False),
        ]
        tail = "Decide if there is a tradeable opportunity. Return STRICT JSON."
        user_without_notes = "\n".join(p for p in [*base_parts, tail] if p)
        notes_block = self._plan_notes_block(_SCAN_SYSTEM, user_without_notes)
        user = "\n".join(p for p in [*base_parts, notes_block, tail] if p)
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
        base_parts = [
            f"pair: {pair}",
            _horizon_guidance(context),
            _position_guidance(context),
            "decision_context:",
            json.dumps(_compact_context(context), ensure_ascii=False),
            "proposed_draft:",
            json.dumps(_draft_summary(draft), ensure_ascii=False),
        ]
        tail = "Make the final decision (accept/revise/reject). Return STRICT JSON."
        user_without_notes = "\n".join(p for p in [*base_parts, tail] if p)
        notes_block = self._plan_notes_block(_FINAL_SYSTEM, user_without_notes)
        user = "\n".join(p for p in [*base_parts, notes_block, tail] if p)
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
    """final_decision プロンプト用に draft を要約。expires_at は ISO 文字列化。

    scale_in / new_signal_evidence (coerce 済の申告値) も渡す — planner は建玉指針で
    これらの妥当性判断を求められるため、要約に無いと判断材料を欠く (Task 9 review)。
    """
    return {
        "direction": draft.direction,
        "entry_conditions": [c.to_dict() for c in draft.entry_conditions],
        "action": draft.action,
        "invalidation": [c.to_dict() for c in draft.invalidation],
        "expires_at": draft.expires_at.isoformat(),
        "reasoning_summary": draft.reasoning_summary,
        "scale_in": draft.scale_in,
        "new_signal_evidence": draft.new_signal_evidence,
    }
