"""プロンプトの建玉・既存 plan 指針 (spec P-3): position / current_plan が
context にある場合だけ scale-in / hold 優先の指針文が入る。"""
from __future__ import annotations

from dataclasses import replace

from src.config.schema import AgentLlm
from src.orchestrator.execution_opinion_agent import (
    ExecutionOpinionAgent,
    _position_guidance,
)
from src.orchestrator.execution_opinion_agent import _compact_context as exec_cc
from src.orchestrator.planner_agent import PlannerAgent, _draft_summary
from src.orchestrator.planner_agent import _compact_context as planner_cc
from src.orchestrator.schemas import ExecutionPlanDraft


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
_FINAL_ACCEPT = (
    '{"decision": "accept", "final_score": 0.7, "confidence": 0.6, '
    '"reasoning_summary": "ok", "revision_request": null}'
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


def test_current_plan_unavailable_dict_yields_no_plan_guidance():
    """unavailable ブロック (plan_id なし) は「既存 plan あり」指針を出さない。

    (実際は P-4 gate が prompt 前に hold するが、guidance 単体でも頑健に。)
    """
    ctx = {"position": {"count": 0, "items": []},
           "current_plan": {"status": "unavailable"}}
    assert _position_guidance(ctx) == ""


def test_position_and_plan_both_present_yields_both_parts():
    """建玉と既存 plan の両方がある場合、両指針が空白連結の単一文字列で入る。"""
    ctx = {"position": {"count": 1, "items": [{"direction": "long"}]},
           "current_plan": {"plan_id": 1, "direction": "long"}}
    g = _position_guidance(ctx)
    assert "scale_in" in g                  # 建玉指針
    assert "existing plan" in g.lower()     # 既存 plan 指針
    assert isinstance(g, str) and "\n" not in g  # space-joined 単一文字列


# ── _draft_summary が scale_in 申告を final_decision に渡す ──


def test_draft_summary_includes_scale_in_fields():
    """final_decision プロンプトの draft 要約に (coerce 済) scale_in / evidence が入る。"""
    draft = replace(
        ExecutionPlanDraft.from_llm_json(_GOOD_DRAFT_JSON),
        scale_in=True,
        new_signal_evidence="fresh H4 breakout above weekly high",
    )
    s = _draft_summary(draft)
    assert s["scale_in"] is True
    assert s["new_signal_evidence"] == "fresh H4 breakout above weekly high"


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


async def test_planner_final_prompt_contains_scale_in_guidance() -> None:
    """final_decision の user プロンプトにも建玉指針 (scale-in) が届く。"""
    llm = _FakeLLM([_FINAL_ACCEPT])
    agent = PlannerAgent(AgentLlm(client=llm, temperature=0.1))
    draft = ExecutionPlanDraft.from_llm_json(_GOOD_DRAFT_JSON)
    await agent.final_decision(
        pair="USDJPY=X", context=_ctx_with_position(), draft=draft,
    )
    user = llm.calls[0][1]["content"]
    assert "scale-in" in user
