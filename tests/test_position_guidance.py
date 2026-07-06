"""プロンプトの建玉・既存 plan 指針 (spec P-3): position / current_plan が
context にある場合だけ scale-in / hold 優先の指針文が入る。"""
from __future__ import annotations

from src.config.schema import AgentLlm
from src.orchestrator.execution_opinion_agent import (
    ExecutionOpinionAgent,
    _position_guidance,
)
from src.orchestrator.execution_opinion_agent import _compact_context as exec_cc
from src.orchestrator.planner_agent import PlannerAgent
from src.orchestrator.planner_agent import _compact_context as planner_cc


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature=0.1) -> str:
        self.calls.append(messages)
        return self._responses.pop(0)


_GOOD_DRAFT_JSON = (
    "{"
    '"direction": "long",'
    '"entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],'
    '"action": {"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "pullback"},'
    '"invalidation": [{"type": "price_below", "value": 148.0}],'
    '"expires_at": "2026-06-21T18:00:00+00:00",'
    '"reasoning_summary": "pullback long"'
    "}"
)
_OPP_YES = (
    '{"opportunity": "yes", "direction": "long", "score": 0.7, '
    '"confidence": 0.6, "reasoning_summary": "breakout"}'
)


def _ctx_with_position() -> dict:
    return {
        "pair": "USDJPY=X",
        "quote": {"mid": 150.0, "spread": 0.02},
        "position": {"count": 1, "items": [{"direction": "long"}]},
        "policy": {"trade_horizon": "swing", "advice_memo": ""},
    }


# ── _position_guidance 単体 ───────────────────────────────────


def test_no_position_no_plan_yields_empty():
    assert _position_guidance({"position": {"count": 0, "items": []}}) == ""


def test_position_present_demands_scale_in_fields():
    ctx = {"position": {"count": 1, "items": [{"direction": "long"}]}}
    g = _position_guidance(ctx)
    assert "scale_in" in g
    assert "new_signal_evidence" in g


def test_current_plan_present_prefers_hold():
    ctx = {"position": {"count": 0, "items": []},
           "current_plan": {"plan_id": 1, "direction": "long"}}
    g = _position_guidance(ctx)
    assert "existing plan" in g.lower()


# ── _compact_context に current_plan が通る ───────────────────


def test_compact_context_includes_current_plan():
    ctx = {"current_plan": {"plan_id": 1}}
    assert planner_cc(ctx)["current_plan"] == {"plan_id": 1}
    assert exec_cc(ctx)["current_plan"] == {"plan_id": 1}


# ── プロンプト組み立てに指針文が届く ─────────────────────────


async def test_exec_draft_prompt_contains_scale_in_guidance() -> None:
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.1))
    await agent.draft(pair="USDJPY=X", direction="long", context=_ctx_with_position())
    user = llm.calls[0][1]["content"]
    assert "scale-in" in user


async def test_planner_scan_prompt_contains_scale_in_guidance() -> None:
    llm = _FakeLLM([_OPP_YES])
    agent = PlannerAgent(AgentLlm(client=llm, temperature=0.1))
    await agent.scan_opportunity(pair="USDJPY=X", context=_ctx_with_position())
    user = llm.calls[0][1]["content"]
    assert "scale-in" in user
