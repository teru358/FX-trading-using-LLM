"""record_trade_complete の strict 化テスト (spec §3.5)。"""
from __future__ import annotations

import pytest

from src.rag.directional_writer import record_trade_complete


class _FailingStore:
    class directional:
        @staticmethod
        def upsert(**kwargs):
            raise RuntimeError("chroma down")


async def _embed(text):
    return [0.0] * 8


class _Order:
    order_id = "o1"
    pair = "USDJPY=X"
    direction = "buy"
    entry_price = 150.0
    close_price = 151.0
    close_reason = "take_profit"
    realized_pnl = 100.0
    signal_reason = "r"


async def test_rag_failure_propagates():
    with pytest.raises(RuntimeError):
        await record_trade_complete(_FailingStore(), _embed, _Order(), "text")
