"""ブリッジ /ohlcv/{symbol} 用 Pydantic モデル。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OhlcvBar(BaseModel):
    time: str          # ISO 8601 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


class OhlcvResponse(BaseModel):
    symbol: str
    interval: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    bars: list[OhlcvBar]
