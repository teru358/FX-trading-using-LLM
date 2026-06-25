"""ExecutionOpinionAgent (Task 2.4) テスト。

design §5.2 Step3。LLM (price_analysis role) で ExecutionPlanDraft を起案する。
LLM 呼び出しは注入された LLMClient (mockable)。parse は schemas.ExecutionPlanDraft。
re-draft 時は revision_request / risk issues をプロンプトに添えて再起案する (Task 2.7 が利用)。
"""
from __future__ import annotations

import pytest

from src.config.schema import AgentLlm
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
from src.orchestrator.schemas import ExecutionPlanDraft, SchemaParseError


class _FakeLLM:
    """chat() を固定 / 連続レスポンスで返す mock。呼び出し履歴と temperature を記録。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.temperatures: list[float] = []

    async def chat(self, messages, temperature=0.1) -> str:
        self.calls.append(messages)
        self.temperatures.append(temperature)
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


def _ctx() -> dict:
    return {
        "pair": "USDJPY=X",
        "quote": {"mid": 150.0, "spread": 0.02},
        "technical": {"status": "ok", "direction": "long", "bias_score": 0.4},
        "news": {"sentiment_score": 0.3, "confidence": 0.6, "top_reasons": []},
        "policy": {"trade_horizon": "swing", "advice_memo": ""},
    }


async def test_draft_parses_into_execution_plan_draft() -> None:
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.1))
    draft = await agent.draft(pair="USDJPY=X", direction="long", context=_ctx())
    assert isinstance(draft, ExecutionPlanDraft)
    assert draft.direction == "long"
    assert draft.action["rr"] == 2.0


async def test_prompt_includes_pair_and_direction() -> None:
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.1))
    await agent.draft(pair="USDJPY=X", direction="short", context=_ctx())
    # system + user message が渡る
    msgs = llm.calls[0]
    blob = " ".join(m["content"] for m in msgs)
    assert "USDJPY=X" in blob
    assert "short" in blob


async def test_malformed_output_raises_schema_parse_error() -> None:
    llm = _FakeLLM(["not json"])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.1))
    with pytest.raises(SchemaParseError):
        await agent.draft(pair="USDJPY=X", direction="long", context=_ctx())


async def test_redraft_includes_revision_feedback() -> None:
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.1))
    await agent.draft(
        pair="USDJPY=X",
        direction="long",
        context=_ctx(),
        revision_feedback=["rr 0.8 below min 1.5", "long sl must be below entry"],
    )
    blob = " ".join(m["content"] for m in llm.calls[0])
    assert "rr 0.8 below min 1.5" in blob


async def test_draft_uses_injected_temperature() -> None:
    """AgentLlm.temperature が chat() に渡る (method 既定 0.1 ではなく注入値)。"""
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.37))
    await agent.draft(pair="USDJPY=X", direction="long", context=_ctx())
    assert llm.temperatures == [0.37]


async def test_draft_explicit_temperature_overrides_injected() -> None:
    llm = _FakeLLM([_GOOD_DRAFT_JSON])
    agent = ExecutionOpinionAgent(AgentLlm(client=llm, temperature=0.37))
    await agent.draft(pair="USDJPY=X", direction="long", context=_ctx(), temperature=0.8)
    assert llm.temperatures == [0.8]
