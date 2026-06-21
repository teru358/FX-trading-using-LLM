"""PlannerAgent (Task 2.5) テスト。

design §5.2 Step2 (opportunity scan) + Step4 (final decision)。同一 snapshot で
2 回 dispatch される。LLM 呼び出しは注入された LLMClient (mockable)。
parse は schemas.PlannerOpportunity / PlannerFinalDecision。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.orchestrator.planner_agent import PlannerAgent
from src.orchestrator.schemas import (
    EntryCondition,
    ExecutionPlanDraft,
    InvalidationCondition,
    PlannerFinalDecision,
    PlannerOpportunity,
    SchemaParseError,
)


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature=0.1) -> str:
        self.calls.append(messages)
        return self._responses.pop(0)


def _ctx() -> dict:
    return {
        "pair": "USDJPY=X",
        "quote": {"mid": 150.0, "spread": 0.02},
        "technical": {"status": "ok", "direction": "long", "bias_score": 0.4},
        "news": {"sentiment_score": 0.3, "confidence": 0.6, "top_reasons": []},
        "policy": {"trade_horizon": "swing", "advice_memo": ""},
    }


def _draft() -> ExecutionPlanDraft:
    return ExecutionPlanDraft(
        direction="long",
        entry_conditions=[EntryCondition(type="price_at_or_below", value=150.0)],
        action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
        invalidation=[InvalidationCondition(type="price_below", value=148.0)],
        expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
        reasoning_summary="r",
    )


_OPP_YES = (
    '{"opportunity": "yes", "direction": "long", "score": 0.7, '
    '"confidence": 0.6, "reasoning_summary": "breakout"}'
)
_OPP_NO = (
    '{"opportunity": "no", "direction": "none", "score": 0.1, '
    '"confidence": 0.2, "reasoning_summary": "no edge"}'
)
_FINAL_ACCEPT = (
    '{"decision": "accept", "final_score": 0.72, "confidence": 0.65, '
    '"reasoning_summary": "aligned"}'
)


# ── opportunity scan (Step2) ──────────────────────────────────


async def test_scan_opportunity_yes() -> None:
    llm = _FakeLLM([_OPP_YES])
    agent = PlannerAgent(llm)
    opp = await agent.scan_opportunity(pair="USDJPY=X", context=_ctx())
    assert isinstance(opp, PlannerOpportunity)
    assert opp.opportunity == "yes"
    assert opp.direction == "long"


async def test_scan_opportunity_no() -> None:
    llm = _FakeLLM([_OPP_NO])
    agent = PlannerAgent(llm)
    opp = await agent.scan_opportunity(pair="USDJPY=X", context=_ctx())
    assert opp.opportunity == "no"


async def test_scan_malformed_raises() -> None:
    llm = _FakeLLM(["garbage"])
    agent = PlannerAgent(llm)
    with pytest.raises(SchemaParseError):
        await agent.scan_opportunity(pair="USDJPY=X", context=_ctx())


# ── final decision (Step4) ────────────────────────────────────


async def test_final_decision_accept() -> None:
    llm = _FakeLLM([_FINAL_ACCEPT])
    agent = PlannerAgent(llm)
    dec = await agent.final_decision(pair="USDJPY=X", context=_ctx(), draft=_draft())
    assert isinstance(dec, PlannerFinalDecision)
    assert dec.decision == "accept"


async def test_final_decision_prompt_includes_draft() -> None:
    llm = _FakeLLM([_FINAL_ACCEPT])
    agent = PlannerAgent(llm)
    await agent.final_decision(pair="USDJPY=X", context=_ctx(), draft=_draft())
    blob = " ".join(m["content"] for m in llm.calls[0])
    # draft の SL/TP が判断材料としてプロンプトに入る
    assert "149" in blob and "152" in blob
