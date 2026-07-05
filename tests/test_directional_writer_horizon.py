"""RAG case card の horizon タグ (spec V-1 write 側): 新規カードにのみ付与。"""
import asyncio
from types import SimpleNamespace

from src.rag.directional_writer import record_trade_entry


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


def _order():
    return SimpleNamespace(
        order_id="o1", pair="USDJPY=X", direction="buy",
        entry_price=150.0, stop_loss=149.5, take_profit=151.0,
    )


def _signal():
    return SimpleNamespace(combined_score=0.4, confidence=0.7, detail_reason="test")


def test_horizon_passed_to_metadata():
    store = _FakeStore()
    asyncio.run(record_trade_entry(store, _embed, _order(), _signal(), horizon="day"))
    assert store.directional.calls[0]["horizon"] == "day"


def test_horizon_omitted_when_none():
    """horizon=None ならキー自体を渡さない (「キー無し = legacy swing」規約)。"""
    store = _FakeStore()
    asyncio.run(record_trade_entry(store, _embed, _order(), _signal()))
    assert "horizon" not in store.directional.calls[0]
