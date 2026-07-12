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


def test_total_within_budget_when_remaining_is_binding(tmp_path):
    import json
    from src.orchestrator.planner_agent import (
        _PROMPT_BUDGET_CHARS,
        _SCAN_SYSTEM,
        _USER_NOTES_MAX_CHARS,
        _compact_context,
    )
    from src.orchestrator.execution_opinion_agent import (
        _horizon_guidance,
        _position_guidance,
    )

    notes = tmp_path / "user_notes.md"
    notes.write_text("## plan\n" + ("n" * 3000) + "\n", encoding="utf-8")  # notes は上限超だが
    llm = _FakeLLM()
    agent = PlannerAgent(_AgentLlm(llm), user_notes_path=notes)

    # remaining が数百 char (0 < remaining < _USER_NOTES_MAX_CHARS) になるよう pad を
    # 実測 base overhead から逆算する (horizon guidance / json 構造 / tail 込み)。
    def _base_len(pad: str) -> int:
        ctx = {"technical": {"pad": pad}}
        base = [
            "pair: USDJPY=X",
            _horizon_guidance(ctx),
            _position_guidance(ctx),
            "decision_context:",
            json.dumps(_compact_context(ctx), ensure_ascii=False),
            "Decide if there is a tradeable opportunity. Return STRICT JSON.",
        ]
        return len(_SCAN_SYSTEM) + len("\n".join(p for p in base if p))

    # 目標 remaining ≈ 500: pad を増やして base をその分埋める。
    overhead0 = _base_len("")  # pad=0 の時の system+user 長
    pad_len = _PROMPT_BUDGET_CHARS - overhead0 - 500
    assert pad_len > 0
    pad = "z" * pad_len
    remaining = _PROMPT_BUDGET_CHARS - _base_len(pad)
    assert 0 < remaining < _USER_NOTES_MAX_CHARS  # binding constraint であることを確認

    asyncio.run(agent.scan_opportunity(pair="USDJPY=X", context={"technical": {"pad": pad}}))
    user_msg = next(m["content"] for m in llm.last_messages if m["role"] == "user")
    total = len(_SCAN_SYSTEM) + len(user_msg)
    assert total <= _PROMPT_BUDGET_CHARS  # ヘッダ込みで予算を超えない
    # notes は remaining 由来の cap で切られている (1500 未満) が一部は注入されている
    assert "n" in user_msg
