from __future__ import annotations
import pytest
from src.rag.ask_context_builder import extract_pairs
from src.rag.ask_context_builder import merge_and_rank_results
from src.rag.ask_context_builder import build_trade_summary


def _make_instruments():
    from dataclasses import dataclass
    @dataclass
    class FakeInstrument:
        symbol: str
        display_name: str
    return [
        FakeInstrument(symbol="USDJPY=X", display_name="USD/JPY"),
        FakeInstrument(symbol="EURUSD=X", display_name="EUR/USD"),
        FakeInstrument(symbol="GBPUSD=X", display_name="GBP/USD"),
    ]

def test_extract_pair_english():
    assert extract_pairs("What about EURUSD?", _make_instruments()) == ["EURUSD=X"]

def test_extract_pair_slash():
    assert extract_pairs("USD/JPY の見通しは？", _make_instruments()) == ["USDJPY=X"]

def test_extract_pair_japanese_dollar_yen():
    assert extract_pairs("ドル円は上がる？", _make_instruments()) == ["USDJPY=X"]

def test_extract_pair_japanese_euro_dollar():
    assert extract_pairs("ユーロドルについて", _make_instruments()) == ["EURUSD=X"]

def test_extract_multiple_pairs():
    result = extract_pairs("EURUSDとUSDJPYの比較", _make_instruments())
    assert "EURUSD=X" in result
    assert "USDJPY=X" in result

def test_extract_no_pair():
    assert extract_pairs("今の相場はどう？", _make_instruments()) == []

def test_extract_pair_case_insensitive():
    assert extract_pairs("eurusd is interesting", _make_instruments()) == ["EURUSD=X"]


# merge_and_rank_results tests

def test_merge_and_rank_by_distance():
    results = [
        {"text": "a", "metadata": {"pair": "EURUSD=X"}, "distance": 0.5, "source": "news"},
        {"text": "b", "metadata": {"pair": "EURUSD=X"}, "distance": 0.1, "source": "bullish"},
        {"text": "c", "metadata": {"pair": "EURUSD=X"}, "distance": 0.3, "source": "reflections"},
    ]
    ranked = merge_and_rank_results(results, max_results=10)
    assert ranked[0]["text"] == "b"
    assert ranked[1]["text"] == "c"
    assert ranked[2]["text"] == "a"

def test_merge_and_rank_limits():
    results = [
        {"text": f"item-{i}", "metadata": {}, "distance": 0.1 * i, "source": "news"}
        for i in range(20)
    ]
    ranked = merge_and_rank_results(results, max_results=5)
    assert len(ranked) == 5


# build_trade_summary tests

def _make_sessions(outcomes):
    from dataclasses import dataclass
    @dataclass
    class FakeSession:
        pair: str
        realized_pnl: float
        outcome: str
        close_reason: str
    return [
        FakeSession(pair=pair, realized_pnl=pnl, outcome="win" if pnl > 0 else "loss", close_reason=reason)
        for pair, pnl, reason in outcomes
    ]

def test_trade_summary_single_pair():
    sessions = _make_sessions([
        ("EURUSD=X", 10.0, "take_profit"),
        ("EURUSD=X", -5.0, "stop_loss"),
        ("EURUSD=X", 8.0, "take_profit"),
    ])
    result = build_trade_summary(sessions, pairs=["EURUSD=X"])
    assert "EURUSD=X" in result
    assert "Win: 2" in result
    assert "Loss: 1" in result

def test_trade_summary_no_pair_gives_total():
    sessions = _make_sessions([
        ("EURUSD=X", 10.0, "take_profit"),
        ("USDJPY=X", -5.0, "stop_loss"),
    ])
    result = build_trade_summary(sessions, pairs=[])
    assert "Overall" in result
    assert "Total: 2" in result or "Total: 1" in result

def test_trade_summary_empty():
    result = build_trade_summary([], pairs=["EURUSD=X"])
    assert "No trade history" in result
