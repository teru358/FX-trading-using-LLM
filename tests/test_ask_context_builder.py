from __future__ import annotations
import pytest
from src.rag.ask_context_builder import extract_pairs
from src.rag.ask_context_builder import merge_and_rank_results


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
