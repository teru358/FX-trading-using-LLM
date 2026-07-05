"""プロンプトの horizon 指針 (spec S-2): day/swing で指針文が切り替わる。"""
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent, _horizon_guidance
from src.orchestrator.planner_agent import _horizon_guidance as planner_guidance


def _ctx(horizon, ttl=8):
    return {"policy": {"trade_horizon": horizon, "advice_memo": None,
                       "plan_ttl_max_hours": ttl}}


def test_day_guidance_mentions_ttl_and_atr():
    text = _horizon_guidance(_ctx("day"))
    assert "DAY" in text
    assert "8 hours" in text
    assert "1h ATR" in text
    assert "RR >= 2" in text


def test_swing_guidance_mentions_days():
    text = _horizon_guidance(_ctx("swing"))
    assert "SWING" in text


def test_day_guidance_without_ttl_omits_ttl_line():
    text = _horizon_guidance(_ctx("day", ttl=0))
    assert "DAY" in text
    assert "hours from now" not in text


def test_exec_user_prompt_contains_guidance():
    class _Llm:
        client = None
        temperature = 0.2
    agent = ExecutionOpinionAgent(_Llm())
    prompt = agent._build_user_prompt("USDJPY=X", "long", _ctx("day"), None)
    assert "DAY" in prompt


def test_planner_guidance_shared_semantics():
    assert "DAY" in planner_guidance(_ctx("day"))
    assert "SWING" in planner_guidance(_ctx("swing"))
