"""strict 化した generate_close_reflection のテスト (spec §3.5/§3.5b)。"""
from __future__ import annotations

import json
import logging

import pytest

from src.analysis.reflector import ReflectionValidationError, generate_close_reflection
from src.config.schema import InstrumentConfig
from src.trading.position_manager import Order
from src.utils.clock import db_now


PAIR = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY")


def _order(direction="buy", entry=150.0, close=151.0, reason="take_profit"):
    return Order(
        order_id="o1", pair="USDJPY=X", direction=direction,
        entry_price=entry, stop_loss=entry - 1.0, take_profit=entry + 2.0,
        position_size=1000, status="closed",
        closed_at=db_now(), close_price=close, close_reason=reason,
        realized_pnl=(close - entry) * 1000 * (1 if direction == "buy" else -1),
        signal_reason="test entry",
    )


class _FakeLLM:
    def __init__(self, response: str | Exception):
        self._response = response

    async def chat(self, messages, temperature=0.0):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _valid_json(**overrides):
    data = {
        "outcome_summary": "TP hit",
        "lesson": "good entry",
        "was_directionally_correct": True,
        "confidence_assessment": "ok",
    }
    data.update(overrides)
    return json.dumps(data)


async def test_success_returns_reflection():
    r = await generate_close_reflection(
        pair_cfg=PAIR, order=_order(), llm=_FakeLLM(_valid_json()))
    assert r.outcome_summary == "TP hit"
    assert "Lesson: good entry" in r.full_text


async def test_llm_exception_propagates():
    with pytest.raises(RuntimeError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM(RuntimeError("timeout")))


async def test_invalid_json_raises():
    with pytest.raises(Exception):   # extract_json の parse 失敗が伝搬する
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM("not json at all"))


@pytest.mark.parametrize("missing", ["outcome_summary", "lesson", "was_directionally_correct"])
async def test_missing_required_key_raises(missing):
    data = json.loads(_valid_json())
    del data[missing]
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM(json.dumps(data)))


async def test_wrong_type_raises():
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(),
            llm=_FakeLLM(_valid_json(was_directionally_correct="yes")))


class TestMachineDirectionJudgment:
    """方向正誤は価格方向の機械判定 (spec §3.5b)。LLM 申告は上書きされる。"""

    async def test_buy_close_above_entry_is_correct(self, caplog):
        caplog.set_level(logging.WARNING, logger="src.analysis.reflector")
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 151.0, "manual"),
            llm=_FakeLLM(_valid_json(was_directionally_correct=False)))
        assert r.was_directionally_correct is True   # 機械判定が勝つ
        # LLM 申告との不一致は warning に残る (spec §3.5b「整合確認」)
        assert any("machine verdict" in rec.message for rec in caplog.records)
        # RAG カード本文は機械判定値を明記する
        assert "directionally_correct=True" in r.full_text

    async def test_buy_close_below_entry_is_incorrect(self):
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 149.0, "stop_loss"),
            llm=_FakeLLM(_valid_json(was_directionally_correct=True)))
        assert r.was_directionally_correct is False

    async def test_sell_close_below_entry_is_correct(self, caplog):
        caplog.set_level(logging.WARNING, logger="src.analysis.reflector")
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("sell", 150.0, 149.0, "take_profit"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
        # 一致時は warning なし + full_text (RAG へ渡す本文) に機械判定値
        assert not any("machine verdict" in rec.message for rec in caplog.records)
        assert "directionally_correct=True" in r.full_text

    async def test_trailing_sl_with_profit_buy_is_correct(self):
        # 旧実装の won = (close_reason == "take_profit") では誤判定していたケース
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 150.8, "profit_lock"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
