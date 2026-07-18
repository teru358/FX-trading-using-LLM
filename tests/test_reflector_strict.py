"""strict 化した generate_close_reflection のテスト (spec §3.5/§3.5b)。"""
from __future__ import annotations

import json
import logging

import pytest

from src.analysis import reflector
from src.analysis.reflector import ReflectionValidationError, generate_close_reflection
from src.config.schema import InstrumentConfig
from src.trading.position_manager import Order
from src.utils.clock import db_now


PAIR = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY")


def _order(direction="buy", entry=150.0, close=151.0, reason="take_profit"):
    pnl = None if close is None else (close - entry) * 1000 * (1 if direction == "buy" else -1)
    return Order(
        order_id="o1", pair="USDJPY=X", direction=direction,
        entry_price=entry, stop_loss=entry - 1.0, take_profit=entry + 2.0,
        position_size=1000, status="closed",
        closed_at=db_now(), close_price=close, close_reason=reason,
        realized_pnl=pnl,
        signal_reason="test entry",
    )


class _FakeLLM:
    def __init__(self, response: str | Exception):
        self._response = response
        self.messages = None

    async def chat(self, messages, temperature=0.0):
        self.messages = messages
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    @property
    def prompt(self) -> str:
        """組み立てられた user プロンプト本文。"""
        assert self.messages is not None, "chat() が呼ばれていない"
        return self.messages[-1]["content"]


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
    # extract_json は JSON ブロック不在時に ValueError を投げる (json.JSONDecodeError も
    # ValueError のサブクラス)。それがそのまま伝搬する。
    with pytest.raises(ValueError):
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


@pytest.mark.parametrize("key", ["outcome_summary", "lesson", "was_directionally_correct"])
async def test_explicit_null_required_key_raises(key):
    """明示的 null は「キーが存在する」ため in 判定を通り抜ける。

    _sanitize_json が壊れた値を能動的に null へ変換するので、null 値の必須キーは
    現実的な LLM 出力。型チェック側で確実に弾く。
    """
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM(_valid_json(**{key: None})))


async def test_non_str_confidence_assessment_raises():
    """confidence_assessment は任意キーだが、存在するなら str でなければならない。"""
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(),
            llm=_FakeLLM(_valid_json(confidence_assessment={"nested": "dict"})))


async def test_missing_confidence_assessment_defaults_to_empty():
    """confidence_assessment 欠落は必須ではないので空文字で通る。"""
    data = json.loads(_valid_json())
    del data["confidence_assessment"]
    r = await generate_close_reflection(
        pair_cfg=PAIR, order=_order(), llm=_FakeLLM(json.dumps(data)))
    assert r.confidence_assessment == ""


async def test_missing_close_price_raises():
    """close_price None は方向判定不能 → 捏造せず retry 機構へ流す (HIGH-1)。

    旧実装は entry_price で補填していたため、機械判定が entry > entry = False に
    化け、誤った directionally_correct=False が RAG に焼き込まれていた。
    """
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(close=None), llm=_FakeLLM(_valid_json()))


class TestPromptAssembly:
    """プロンプト組み立ての回帰検証 (旧 test_reflector.py から移植)。"""

    async def test_buy_win_achieved_rr_is_positive(self):
        # entry 150 / SL 149 (1.00 risk) / close 152 → achieved_rr = +2.00
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 152.0, "take_profit"), llm=llm)
        assert "Achieved R:R: +2.00" in llm.prompt

    async def test_buy_loss_achieved_rr_is_negative(self):
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 149.0, "stop_loss"), llm=llm)
        assert "Achieved R:R: -1.00" in llm.prompt

    async def test_sell_win_achieved_rr_is_positive(self):
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order("sell", 150.0, 148.0, "take_profit"), llm=llm)
        assert "Achieved R:R: +2.00" in llm.prompt

    async def test_sell_loss_achieved_rr_is_negative(self):
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order("sell", 150.0, 151.0, "stop_loss"), llm=llm)
        assert "Achieved R:R: -1.00" in llm.prompt

    async def test_entry_analysis_section_rendered_when_given(self):
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=llm,
            entry_analysis="planner said trend continuation")
        assert "=== Entry Analysis (Full Context) ===" in llm.prompt
        assert "planner said trend continuation" in llm.prompt

    async def test_entry_analysis_section_absent_when_empty(self):
        llm = _FakeLLM(_valid_json())
        await generate_close_reflection(pair_cfg=PAIR, order=_order(), llm=llm)
        assert "=== Entry Analysis" not in llm.prompt
        # 退役したセクションが復活していないこと (spec §3.3)
        assert "Macro Instruments" not in llm.prompt
        assert "SL/TP Analysis" not in llm.prompt
        assert "Parameter History" not in llm.prompt
        assert "atr_params_suggestion" not in llm.prompt


class TestMachineDirectionJudgment:
    """方向正誤は価格方向の機械判定 (spec §3.5b)。LLM 申告は上書きされる。"""

    async def test_buy_close_above_entry_is_correct(self, caplog):
        caplog.set_level(logging.WARNING, logger=reflector.__name__)
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
        caplog.set_level(logging.WARNING, logger=reflector.__name__)
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("sell", 150.0, 149.0, "take_profit"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
        # 一致時は warning なし + full_text (RAG へ渡す本文) に機械判定値
        assert not any("machine verdict" in rec.message for rec in caplog.records)
        assert "directionally_correct=True" in r.full_text

    @pytest.mark.parametrize("direction", ["buy", "sell"])
    async def test_breakeven_close_is_incorrect(self, direction):
        """建値決済 (close == entry) は「方向的に正しくなかった」に倒す (意図的)。

        利益が出ていない以上、方向の優位性は確認できなかったという扱い。
        三値化しない判断は spec §3.4 の BOOLEAN NULL 規定との衝突回避のため。
        """
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order(direction, 150.0, 150.0, "manual"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is False

    async def test_trailing_sl_with_profit_buy_is_correct(self):
        # 旧実装の won = (close_reason == "take_profit") では誤判定していたケース
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 150.8, "profit_lock"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
