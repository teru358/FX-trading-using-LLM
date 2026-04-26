"""内部正規形 (yfinance "USDJPY=X") と MT5 形式 ("USDJPY") の双方向変換。

なぜ双方向必要か:
- 送信時 (発注 payload 組立、quote リクエスト 等): system → MT5
- 受信時 (Phase 3b の position reconciliation、Phase 4 の MT5 OHLCV 取込み): MT5 → system

Phase 3a では `to_mt5_symbol()` のみ実利用、`from_mt5_symbol()` は先取り実装。
"""
from __future__ import annotations


def to_mt5_symbol(yf_symbol: str) -> str:
    """内部正規形 (USDJPY=X) を MT5 形式 (USDJPY) に変換。

    既に MT5 形式 (=X なし) ならそのまま返す。空文字なら空文字を返す。
    """
    if not yf_symbol:
        return ""
    return yf_symbol.removesuffix("=X")


def from_mt5_symbol(mt5_symbol: str) -> str:
    """MT5 形式 (USDJPY) を内部正規形 (USDJPY=X) に変換。

    既に =X 付きならそのまま返す (idempotent)。空文字なら空文字を返す。
    """
    if not mt5_symbol:
        return ""
    if mt5_symbol.endswith("=X"):
        return mt5_symbol
    return mt5_symbol + "=X"
