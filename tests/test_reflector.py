"""reflector.generate_close_reflection() のユニットテスト。

LLM 呼び出しはモックし、achieved_rr の符号付き計算・
プロンプト構築・フォールバック動作を検証する。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.analysis.reflector import generate_close_reflection, Reflection
from src.config import InstrumentConfig
from src.trading.position_manager import Order


def _pair_cfg() -> InstrumentConfig:
    return InstrumentConfig(
        symbol="USDJPY=X",
        display_name="USD/JPY",
        asset_type="fx",
        mode="trade",
        base_currency="USD",
        quote_currency="JPY",
    )


def _order(direction: str, entry: float, sl: float, tp: float,
           close_price: float, close_reason: str) -> Order:
    o = Order.new(
        pair="USDJPY=X",
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        position_size=10000.0,
        signal_reason="test signal",
    )
    o.close_price = close_price
    o.close_reason = close_reason
    o.realized_pnl = (close_price - entry) * 100 if direction == "buy" else (entry - close_price) * 100
    o.closed_at = o.opened_at + timedelta(hours=48)
    return o


def _mock_llm(response: str) -> AsyncMock:
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=response)
    return llm


# ── achieved_rr 符号テスト ────────────────────────────────


@pytest.mark.asyncio
async def test_buy_win_positive_rr():
    """BUY でTP到達 → achieved_rr が正。"""
    order = _order("buy", entry=150.0, sl=149.0, tp=152.0,
                   close_price=152.0, close_reason="take_profit")
    llm = _mock_llm('{"outcome_summary":"TP hit","was_directionally_correct":true,"lesson":"good","confidence_assessment":"ok"}')

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    assert ref.was_directionally_correct is True
    # TP方向に2.0円動いた、SL距離1.0円 → RR = +2.0
    prompt_text = llm.chat.call_args[0][0][1]["content"]
    assert "+2.00" in prompt_text  # Achieved R:R: +2.00


@pytest.mark.asyncio
async def test_buy_loss_negative_rr():
    """BUY でSL到達 → achieved_rr が負。"""
    order = _order("buy", entry=150.0, sl=149.0, tp=152.0,
                   close_price=149.0, close_reason="stop_loss")
    llm = _mock_llm('{"outcome_summary":"SL hit","was_directionally_correct":false,"lesson":"bad","confidence_assessment":"poor"}')

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    assert ref.was_directionally_correct is False
    prompt_text = llm.chat.call_args[0][0][1]["content"]
    assert "-1.00" in prompt_text  # Achieved R:R: -1.00


@pytest.mark.asyncio
async def test_sell_win_positive_rr():
    """SELL でTP到達 → achieved_rr が正。"""
    order = _order("sell", entry=150.0, sl=151.0, tp=148.0,
                   close_price=148.0, close_reason="take_profit")
    llm = _mock_llm('{"outcome_summary":"TP hit","was_directionally_correct":true,"lesson":"good","confidence_assessment":"ok"}')

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    prompt_text = llm.chat.call_args[0][0][1]["content"]
    assert "+2.00" in prompt_text


@pytest.mark.asyncio
async def test_sell_loss_negative_rr():
    """SELL でSL到達 → achieved_rr が負。"""
    order = _order("sell", entry=150.0, sl=151.0, tp=148.0,
                   close_price=151.0, close_reason="stop_loss")
    llm = _mock_llm('{"outcome_summary":"SL hit","was_directionally_correct":false,"lesson":"bad","confidence_assessment":"poor"}')

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    prompt_text = llm.chat.call_args[0][0][1]["content"]
    assert "-1.00" in prompt_text


# ── LLM 失敗時のフォールバック ────────────────────────────


@pytest.mark.asyncio
async def test_llm_failure_returns_factual_fallback():
    """LLM が例外を投げた場合、ファクチュアルフォールバックを返す。"""
    order = _order("buy", entry=150.0, sl=149.0, tp=152.0,
                   close_price=152.0, close_reason="take_profit")
    llm = _mock_llm("")
    llm.chat = AsyncMock(side_effect=Exception("LLM down"))

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    assert isinstance(ref, Reflection)
    assert ref.was_directionally_correct is True  # TP hit
    assert "TAKE PROFIT" in ref.outcome_summary


# ── macro_context 注入 ────────────────────────────────────


@pytest.mark.asyncio
async def test_macro_context_included_in_prompt():
    """macro_context_at_entry が渡されるとプロンプトに含まれる。"""
    order = _order("buy", entry=150.0, sl=149.0, tp=152.0,
                   close_price=151.0, close_reason="manual")
    llm = _mock_llm('{"outcome_summary":"manual close","was_directionally_correct":true,"lesson":"ok","confidence_assessment":"ok"}')

    await generate_close_reflection(
        _pair_cfg(), order, llm,
        macro_context_at_entry="S&P500: bullish +0.3, DXY: bearish -0.2",
    )

    prompt_text = llm.chat.call_args[0][0][1]["content"]
    assert "S&P500: bullish" in prompt_text
    assert "Macro Instruments at Entry" in prompt_text


# ── ATR params suggestion ────────────────────────────────


@pytest.mark.asyncio
async def test_atr_params_suggestion_parsed():
    """LLMがatr_params_suggestionを返した場合、Reflectionに格納される。"""
    order = _order("buy", entry=150.0, sl=149.0, tp=152.0,
                   close_price=149.0, close_reason="stop_loss")
    response = (
        '{"outcome_summary":"SL too tight",'
        '"was_directionally_correct":false,'
        '"lesson":"widen SL",'
        '"confidence_assessment":"poor",'
        '"atr_params_suggestion":{"sl_atr_mult":2.5,"tp_atr_mult":null,"reason":"SL too tight"}}'
    )
    llm = _mock_llm(response)

    ref = await generate_close_reflection(_pair_cfg(), order, llm)

    assert ref.atr_params_suggestion is not None
    assert ref.atr_params_suggestion["sl_atr_mult"] == 2.5
    assert ref.atr_params_suggestion["tp_atr_mult"] is None
