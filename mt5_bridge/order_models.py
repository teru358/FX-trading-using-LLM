"""/order エンドポイントの request/response モデル (Pydantic v2)。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    volume_lots: float = Field(gt=0)        # MT5 ネイティブ単位 (1.0 = 1 lot = 100,000 通貨)
    sl: float | None = None                 # stop loss 価格 (0 / None で無効)
    tp: float | None = None                 # take profit 価格
    magic: int = 0                          # bot 識別 ID
    comment: str = ""                       # 任意の備考 (32 文字推奨)


class OrderResponse(BaseModel):
    ticket: int                             # MT5 ticket (DRY_RUN なら time_ns ベースの擬似値)
    symbol: str
    side: Literal["buy", "sell"]
    volume_lots: float
    fill_price: float                       # buy = ask, sell = bid
    sl: float | None
    tp: float | None
    time: str                               # ISO 8601
    dry_run: bool
    magic: int
