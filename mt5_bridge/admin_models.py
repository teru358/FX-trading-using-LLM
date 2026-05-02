"""ブリッジ管理エンドポイント (/admin/*) 用 Pydantic モデル。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HaltRequest(BaseModel):
    mode: Literal["soft", "hard"] = "soft"
    reason: str = ""


class AdminStatus(BaseModel):
    dry_run: bool
    soft_halted: bool
    is_hard_halted: bool
    accepts_new_orders: bool
    mt5_connected: bool
