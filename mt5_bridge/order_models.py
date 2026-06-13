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


class ClosePositionResponse(BaseModel):
    ticket: int
    close_price: float                      # symbol 指定時は (bid+ask)/2、未指定なら 0.0
    time: str                               # ISO 8601
    dry_run: bool
    note: str = ""                          # DRY_RUN モード時の補足説明


class ClosedDealResponse(BaseModel):
    ticket: int
    close_price: float
    profit: float
    swap: float = 0.0
    commission: float = 0.0
    closed_at: str                          # ISO 8601
    reason: str = ""


class ModifyPositionRequest(BaseModel):
    sl: float | None = None
    tp: float | None = None


class ModifyPositionResponse(BaseModel):
    ticket: int
    symbol: str
    sl: float | None
    tp: float | None
    retcode: int | None = None
    comment: str = ""
    dry_run: bool = False
