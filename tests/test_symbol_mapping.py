"""内部正規形 (USDJPY=X) と MT5 形式 (USDJPY) の双方向変換ヘルパーテスト。"""
from __future__ import annotations

from src.trading.symbol_mapping import from_mt5_symbol, to_mt5_symbol


# ── to_mt5_symbol (送信: system → MT5) ────────────────────────────


def test_to_mt5_strips_equals_x_suffix():
    assert to_mt5_symbol("USDJPY=X") == "USDJPY"
    assert to_mt5_symbol("EURUSD=X") == "EURUSD"


def test_to_mt5_returns_unchanged_when_no_suffix():
    assert to_mt5_symbol("USDJPY") == "USDJPY"


def test_to_mt5_empty_returns_empty():
    assert to_mt5_symbol("") == ""


# ── from_mt5_symbol (受信: MT5 → system) ──────────────────────────


def test_from_mt5_adds_equals_x():
    assert from_mt5_symbol("USDJPY") == "USDJPY=X"
    assert from_mt5_symbol("EURUSD") == "EURUSD=X"


def test_from_mt5_idempotent_when_already_canonical():
    """既に =X 付きならそのまま (重複付加を防ぐ)。"""
    assert from_mt5_symbol("USDJPY=X") == "USDJPY=X"


def test_from_mt5_empty_returns_empty():
    assert from_mt5_symbol("") == ""


# ── 往復変換の整合性 ──────────────────────────────────────────────


def test_roundtrip_preserves_symbol():
    for s in ["USDJPY=X", "EURUSD=X", "GBPUSD=X"]:
        assert from_mt5_symbol(to_mt5_symbol(s)) == s
