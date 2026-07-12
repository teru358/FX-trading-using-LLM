# tests/test_planner_user_notes.py
import asyncio
from pathlib import Path

from src.orchestrator.planner_agent import PlannerAgent


class _FakeLLM:
    def __init__(self):
        self.last_messages = None

    async def chat(self, messages, temperature=0.1):
        self.last_messages = messages
        return '{"opportunity":"no","direction":"none","score":0,"confidence":0,"reasoning_summary":"x","missing_inputs":[]}'


class _AgentLlm:
    def __init__(self, client):
        self.client = client
        self.temperature = 0.1


def test_plan_notes_injected_into_user_prompt(tmp_path):
    notes = tmp_path / "user_notes.md"
    notes.write_text("## plan\nprefer trend continuation\n", encoding="utf-8")
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm), user_notes_path=notes)
    asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context={"technical": {}}))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    assert "prefer trend continuation" in user_msg
    assert "advisory" in user_msg.lower()


def test_system_prompt_has_advisory_rule():
    from src.orchestrator.planner_agent import _SCAN_SYSTEM, _FINAL_SYSTEM
    for sysp in (_SCAN_SYSTEM, _FINAL_SYSTEM):
        assert "advisory" in sysp.lower() or "参考" in sysp
        assert "data" in sysp.lower() or "market" in sysp.lower()


def test_notes_truncated_to_limit(tmp_path):
    notes = tmp_path / "user_notes.md"
    notes.write_text("## plan\n" + ("x" * 5000) + "\n", encoding="utf-8")
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm), user_notes_path=notes)
    asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context={}))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    notes_block = user_msg.split("User notes (advisory, data takes priority):\n", 1)[1]
    assert notes_block.count("x") <= 1500


def test_notes_dropped_when_budget_exhausted(tmp_path, caplog):
    import logging
    notes = tmp_path / "user_notes.md"
    notes.write_text("## plan\nMARKER_NOTE\n", encoding="utf-8")
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm), user_notes_path=notes)
    huge_context = {"technical": {"pad": "y" * 20_000}}
    with caplog.at_level(logging.WARNING):
        asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context=huge_context))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    assert "MARKER_NOTE" not in user_msg
    assert any("notes" in r.message.lower() for r in caplog.records)


def test_total_prompt_within_budget_with_typical_context(tmp_path):
    from src.orchestrator.planner_agent import _PROMPT_BUDGET_CHARS, _SCAN_SYSTEM
    notes = tmp_path / "user_notes.md"
    notes.write_text("## plan\nprefer trend continuation\n", encoding="utf-8")
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm), user_notes_path=notes)
    typical_context = {
        "technical": {"status": "ok", "bias_score": 0.42, "confidence": 0.61,
                      "direction": "long",
                      "mtf": {"alignment": 0.67,
                              "long": {"dir": "long", "score": 0.55},
                              "medium": {"dir": "long", "score": 0.31},
                              "short": {"dir": "neutral", "score": 0.02}},
                      "patterns": ["engulfing_bullish"],
                      "components": {"sma": 0.3, "rsi": -0.1, "macd": 0.5,
                                     "ichimoku": 0.2, "bb": 0.0, "pattern": 0.4,
                                     "adx_factor": 1.2}},
        "news": {"status": "ok", "impact": 0.4},
        "quote": {"bid": 157.001, "ask": 157.004},
    }
    asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context=typical_context))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    assert len(_SCAN_SYSTEM) + len(user_msg) <= _PROMPT_BUDGET_CHARS


def test_no_notes_path_no_injection():
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm))  # user_notes_path 省略
    asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context={}))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    assert "advisory" not in user_msg.lower()
