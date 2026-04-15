"""audit_lesson_generator のテスト。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.audit_lesson_generator import generate_candidates
from src.analysis.audit_post_hoc import Counterfactuals, PostHocResult
from tests.fixtures.audit import make_fake_session


def _mk_ph() -> PostHocResult:
    return PostHocResult(
        mfe_during_trade=100, mae_during_trade=-2000,
        mfe_after_close_24h=200, mae_after_close_24h=-500,
        duration_seconds=7200, has_post_close_data=True,
    )


def _mk_cf() -> Counterfactuals:
    return Counterfactuals(
        tp_plus_0_5_atr_hit=False, tp_plus_0_5_atr_pnl=0.0,
        tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=0.0,
        sl_minus_0_5_atr_hit=True, sl_minus_0_5_atr_pnl=-1500.0,
        tighter_sl_would_recover=False,
    )


@pytest.mark.asyncio
async def test_generate_candidates_success():
    """LLM が 2 件の候補を返す。"""
    fake_response = (
        '{"candidates": ['
        '{"rule_text": "Rule A", "rationale": "Because A", "applicability": "all"},'
        '{"rule_text": "Rule B", "rationale": "Because B", "applicability": "USDJPY"}'
        ']}'
    )
    llm_mock = MagicMock()
    llm_mock.chat = AsyncMock(return_value=fake_response)

    s = make_fake_session("t1", pnl=-2000, conf=0.82)
    candidates = await generate_candidates(
        session=s, post_hoc=_mk_ph(), counterfactuals=_mk_cf(),
        vol_percentile=50.0, llm=llm_mock, hint="",
    )
    assert len(candidates) == 2
    assert candidates[0].rule_text == "Rule A"
    assert candidates[1].applicability == "USDJPY"


@pytest.mark.asyncio
async def test_generate_candidates_empty_list():
    """LLM が空配列を返した場合、空リスト。"""
    llm_mock = MagicMock()
    llm_mock.chat = AsyncMock(return_value='{"candidates": []}')
    s = make_fake_session("t1")
    result = await generate_candidates(
        session=s, post_hoc=_mk_ph(), counterfactuals=_mk_cf(),
        vol_percentile=None, llm=llm_mock, hint="",
    )
    assert result == []


@pytest.mark.asyncio
async def test_generate_candidates_malformed_json():
    """LLM が壊れた JSON を返したら空リスト + 警告ログ。"""
    llm_mock = MagicMock()
    llm_mock.chat = AsyncMock(return_value="not a json")
    s = make_fake_session("t1")
    result = await generate_candidates(
        session=s, post_hoc=_mk_ph(), counterfactuals=_mk_cf(),
        vol_percentile=None, llm=llm_mock, hint="",
    )
    assert result == []


@pytest.mark.asyncio
async def test_generate_candidates_with_hint():
    """hint が LLM に渡される。"""
    captured = {}

    async def _capture(messages, **kwargs):
        captured["messages"] = messages
        return '{"candidates": []}'

    llm_mock = MagicMock()
    llm_mock.chat = AsyncMock(side_effect=_capture)

    s = make_fake_session("t1")
    await generate_candidates(
        session=s, post_hoc=_mk_ph(), counterfactuals=_mk_cf(),
        vol_percentile=None, llm=llm_mock, hint="focus on CPI timing",
    )
    user_msg = next(m for m in captured["messages"] if m["role"] == "user")
    assert "focus on CPI timing" in user_msg["content"]
