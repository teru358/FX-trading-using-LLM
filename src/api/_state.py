"""API モジュール内の共有ステート + 認証ヘルパー。

FastAPI のルートが使う共通オブジェクト (config / store / llm_slot 等) を
モジュールグローバルに保持する。``start_api_server`` が起動時に注入し、
各 routes/*.py モジュールが ``state`` 経由で参照する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from src.concurrency.priority_job_slot import PriorityJobSlot
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.rag.vector_store import VectorStore


@dataclass
class APIState:
    """API ハンドラから参照する共有オブジェクト群 (singleton)。"""
    config: AppConfig | None = None
    store: VectorStore | None = None
    analysis_store: AnalysisStore | None = None
    llm_slot: PriorityJobSlot | None = None
    price_store: PriceStore | None = None
    hold_store: Any = None        # HoldDecisionStore
    forecast_store: Any = None    # ForecastStore
    started_at: datetime | None = None


state = APIState()


_api_key_header = APIKeyHeader(name="X-API-Key")


def verify_api_key(api_key: str = Security(_api_key_header)) -> str:
    """X-API-Key ヘッダーを検証する FastAPI Security 依存。"""
    expected = os.environ.get("API_SECRET_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="API_SECRET_KEY not configured")
    if api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
