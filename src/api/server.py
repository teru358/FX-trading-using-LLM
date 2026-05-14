"""REST API サーバー (FastAPI + uvicorn)。

メインプロセスのバックグラウンドスレッドとして起動し、死活確認・ポジション照会・
ニュース状況・緊急決済を提供する。

このファイル自身は **ルーティング登録 + 起動エントリー** のみを担当する。
個々のエンドポイント実装は ``src/api/routes/`` 配下に分割されている:

- ``routes.health``  — /status (プロセス + halt + サブシステム健全性 統合), /logs, /usage, /schedule
- ``routes.account`` — /account (残高 + ポジション一覧、halt は /status へ集約)
- ``routes.admin``   — /admin/halt, /admin/resume (MT5 bridge へのプロキシ)
- ``routes.data``    — /news, /tech, /analyze, /forecast, /feeds
- ``routes.trading`` — /run/trade, /close/{pair}
- ``routes.ask``     — /ask

共有ステート (config, store, llm_slot 等) は ``src/api/_state.py`` の
``state`` シングルトンに保持する。
"""
from __future__ import annotations

import logging
import threading

from fastapi import FastAPI

from src.api._state import state
from src.api.routes import account, admin, ask, data, health, trading
from src.concurrency.priority_job_slot import PriorityJobSlot
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.rag.vector_store import VectorStore
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


app = FastAPI(title="FX Trading Bot API", docs_url=None, redoc_url=None)
app.include_router(health.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(data.router)
app.include_router(trading.router)
app.include_router(ask.router)


# uvicorn のログ設定:
#   uvicorn / uvicorn.error は WARNING のみ (起動ノイズを抑制)
#   uvicorn.access は INFO + propagate=True → 既存の finance.log に流す
_UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "uvicorn":        {"level": "WARNING", "propagate": True},
        "uvicorn.error":  {"level": "WARNING", "propagate": False},
        "uvicorn.access": {"level": "INFO",    "propagate": True},
    },
}


def start_api_server(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    llm_slot: PriorityJobSlot,
    price_store: PriceStore,
    hold_store,        # HoldDecisionStore
    forecast_store,    # ForecastStore
) -> threading.Thread:
    """バックグラウンドスレッドで uvicorn を起動する。"""
    state.config = config
    state.store = store
    state.analysis_store = analysis_store
    state.llm_slot = llm_slot
    state.price_store = price_store
    state.hold_store = hold_store
    state.forecast_store = forecast_store
    state.started_at = db_now()

    def _run() -> None:
        import uvicorn

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=config.api.port,
            log_config=_UVICORN_LOG_CONFIG,
        )

    thread = threading.Thread(target=_run, daemon=True, name="api-server")
    thread.start()
    logger.info(f"REST API server started on 0.0.0.0:{config.api.port}")
    return thread
