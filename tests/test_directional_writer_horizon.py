"""RAG case card の horizon タグ (spec V-1 write 側): 新規カードにのみ付与。

Task 8 で ``record_trade_entry`` (entry フェーズ) が退役したため、同じ
「キー無し = legacy swing」規約を持つ ``record_trade_complete`` を検証対象に
移した。規約自体は変わっていない。
"""
import asyncio
from types import SimpleNamespace

from src.rag.directional_writer import record_trade_complete


class _FakeDirectional:
    def __init__(self):
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)


class _FakeStore:
    def __init__(self):
        self.directional = _FakeDirectional()


async def _embed(_text):
    return [0.0] * 8


def _closed_order():
    return SimpleNamespace(
        order_id="o1", pair="USDJPY=X", direction="buy",
        entry_price=150.0, close_price=151.0, realized_pnl=12.0,
        close_reason="take_profit", signal_reason="test",
    )


def test_horizon_passed_to_metadata():
    store = _FakeStore()
    asyncio.run(record_trade_complete(store, _embed, _closed_order(), "reflect", horizon="day"))
    assert store.directional.calls[0]["horizon"] == "day"


def test_horizon_omitted_when_none():
    """horizon=None ならキー自体を渡さない (「キー無し = legacy swing」規約)。"""
    store = _FakeStore()
    asyncio.run(record_trade_complete(store, _embed, _closed_order(), "reflect"))
    assert "horizon" not in store.directional.calls[0]
