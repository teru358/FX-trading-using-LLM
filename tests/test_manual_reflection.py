"""src.trading.manual_reflection のテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.trading.manual_reflection import run_manual_reflection
from src.trading.position_manager import Order


def _make_closed_order(pair: str = "USDJPY=X") -> Order:
    order = Order.new(
        pair=pair,
        direction="buy",
        entry_price=150.0,
        stop_loss=149.5,
        take_profit=151.0,
        position_size=5000,
        signal_reason="manual test",
    )
    order.opened_at = datetime.now() - timedelta(hours=4)
    order.closed_at = datetime.now()
    order.close_price = 150.8
    order.close_reason = "manual"
    order.realized_pnl = 4000.0
    order.status = "closed"
    return order


def _fake_config(pair: str = "USDJPY=X"):
    inst = SimpleNamespace(symbol=pair, display_name="USD/JPY")
    return SimpleNamespace(
        tradeable_instruments=[inst],
        llm=SimpleNamespace(
            reflection=SimpleNamespace(temperature=0.1),
            ollama=SimpleNamespace(base_url="http://localhost:11434"),
        ),
        rag=SimpleNamespace(embedding_model="nomic-embed-text"),
        user_notes_path=None,
    )


@pytest.mark.asyncio
async def test_run_manual_reflection_unknown_pair_is_noop():
    """未知ペアは警告ログを出して早期 return する (LLM 呼び出しなし)。"""
    cfg = _fake_config(pair="USDJPY=X")
    store = MagicMock()
    order = _make_closed_order(pair="EURUSD=X")  # cfg にないペア

    with patch("src.trading.manual_reflection.create_llm_client") as llm_factory:
        await run_manual_reflection(cfg, store, order)
        llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_run_manual_reflection_happy_path_calls_rag_upsert():
    """正常系: reflection 生成後に record_trade_complete が呼ばれる。"""
    cfg = _fake_config()
    store = MagicMock()
    order = _make_closed_order()

    fake_reflection = SimpleNamespace(
        full_text="Test reflection output",
        atr_params_suggestion=None,
    )

    with (
        patch("src.trading.manual_reflection.create_llm_client", return_value=MagicMock()),
        patch("src.trading.manual_reflection.load_user_notes", return_value=""),
        patch("src.trading.manual_reflection.generate_close_reflection",
              new=AsyncMock(return_value=fake_reflection)) as gen_mock,
        patch("src.trading.manual_reflection.record_trade_complete",
              new=AsyncMock(return_value=None)) as rag_mock,
    ):
        await run_manual_reflection(cfg, store, order)
        gen_mock.assert_awaited_once()
        rag_mock.assert_awaited_once()
        # record_trade_complete の第 3 引数が closed_order、第 4 引数が reflection text
        call_args = rag_mock.await_args
        assert call_args.args[2] is order
        assert call_args.args[3] == "Test reflection output"


@pytest.mark.asyncio
async def test_run_manual_reflection_reflection_failure_does_not_raise():
    """reflection 生成が例外を投げても呼び出し側に伝播しない。"""
    cfg = _fake_config()
    store = MagicMock()
    order = _make_closed_order()

    with (
        patch("src.trading.manual_reflection.create_llm_client", return_value=MagicMock()),
        patch("src.trading.manual_reflection.load_user_notes", return_value=""),
        patch("src.trading.manual_reflection.generate_close_reflection",
              new=AsyncMock(side_effect=RuntimeError("LLM error"))),
        patch("src.trading.manual_reflection.record_trade_complete",
              new=AsyncMock()) as rag_mock,
    ):
        await run_manual_reflection(cfg, store, order)  # 例外を投げないことが重要
        rag_mock.assert_not_awaited()  # 生成失敗したので upsert は呼ばれない
