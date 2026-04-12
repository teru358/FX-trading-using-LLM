"""TradingView シンボル変換ヘルパー。"""
from __future__ import annotations

_SYMBOL_MAP = {
    "USDJPY=X": "FX:USDJPY",
    "EURUSD=X": "FX:EURUSD",
    "GBPUSD=X": "FX:GBPUSD",
}


def to_tv_symbol(yf_symbol: str) -> str:
    """yfinance シンボルを TradingView 形式に変換する。"""
    return _SYMBOL_MAP.get(yf_symbol, yf_symbol)


def to_tv_ticker(yf_symbol: str) -> str:
    """yfinance シンボルから TradingView ticker 部分を返す (syminfo.ticker 用)。

    例: ``"USDJPY=X" → "USDJPY"``, ``"EURUSD=X" → "EURUSD"``
    """
    tv = to_tv_symbol(yf_symbol)
    return tv.split(":")[-1] if ":" in tv else tv
