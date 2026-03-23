"""REST API サーバー（FastAPI + uvicorn）。

メインプロセスのバックグラウンドスレッドとして起動し、
死活確認・ポジション照会・ニュース状況・緊急決済を提供する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader

from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.rag.vector_store import VectorStore
from src.trading.position_manager import PositionManager

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key")

# ── 起動時刻（health 用） ────────────────────────────────────────
_started_at: datetime | None = None

# ── 共有オブジェクト（start_api_server で注入） ──────────────────
_config: AppConfig | None = None
_store: VectorStore | None = None
_analysis_store: AnalysisStore | None = None
_job_lock: threading.Lock | None = None

app = FastAPI(title="FX Trading Bot API", docs_url=None, redoc_url=None)


# ── 認証 ─────────────────────────────────────────────────────────

def _verify_api_key(api_key: str = Security(_api_key_header)) -> str:
    expected = os.environ.get("API_SECRET_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="API_SECRET_KEY not configured")
    if api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key


# ── GET /health ──────────────────────────────────────────────────

@app.get("/health", dependencies=[Depends(_verify_api_key)])
def health() -> dict[str, Any]:
    """プロセス死活確認 + スケジューラ状態。"""
    import schedule as sched_mod

    jobs = sched_mod.get_jobs()
    next_run = min((j.next_run for j in jobs), default=None) if jobs else None

    return {
        "status": "ok",
        "started_at": _started_at.isoformat() if _started_at else None,
        "now": datetime.now().isoformat(),
        "scheduler": {
            "jobs_count": len(jobs),
            "next_run": next_run.isoformat() if next_run else None,
        },
    }


# ── GET /status ──────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(_verify_api_key)])
def status() -> dict[str, Any]:
    """残高・ポジション一覧。"""
    assert _config is not None
    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance)
    account = pm.get_account_state()

    positions = []
    for pos in account.open_positions:
        entry: dict[str, Any] = {
            "pair": pos.pair,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "position_size": pos.position_size,
            "opened_at": pos.opened_at.isoformat(),
        }
        try:
            current = fetch_current_price(pos.pair)
            mult = 1 if pos.direction == "buy" else -1
            entry["current_price"] = current
            entry["unrealized_pnl"] = round(
                (current - pos.entry_price) * pos.position_size * mult, 2
            )
        except Exception:
            entry["current_price"] = None
            entry["unrealized_pnl"] = None
        positions.append(entry)

    pnl = account.balance - account.initial_balance
    return {
        "balance": account.balance,
        "initial_balance": account.initial_balance,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / account.initial_balance * 100, 2),
        "total_trades": account.total_trades,
        "win_rate": round(account.win_rate() * 100, 1),
        "open_positions": positions,
    }


# ── GET /news/latest ─────────────────────────────────────────────

@app.get("/news/latest", dependencies=[Depends(_verify_api_key)])
def news_latest() -> dict[str, Any]:
    """カテゴリ別の最新ニュースセンチメント。"""
    assert _store is not None and _config is not None

    result: dict[str, Any] = {}
    for category in ("fx", "global", "japan"):
        entries = _store.get_recent_category_news(
            categories=[category],
            lookback_hours=_config.rag.news_lookback_hours,
        )
        if entries:
            latest = entries[0]["metadata"]
            result[category] = {
                "sentiment_score": latest.get("sentiment_score"),
                "confidence": latest.get("confidence"),
                "summary": latest.get("summary", ""),
                "key_themes": latest.get("key_themes", ""),
                "news_count": latest.get("news_count"),
                "collected_at": latest.get("collected_at"),
            }
        else:
            result[category] = None

    return {"categories": result}


# ── GET /analyze ─────────────────────────────────────────────────

@app.get("/analyze", dependencies=[Depends(_verify_api_key)])
def analyze() -> dict[str, Any]:
    """保存済みスナップショット＋ニュースから総合シグナルを返す（新規LLM取得なし）。"""
    assert _config is not None and _store is not None and _analysis_store is not None

    from src.trading_cycle import _summarize_pair

    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance)

    async def _gather():
        results = await asyncio.gather(
            *[_summarize_pair(p, _config, pm, _store, _analysis_store)
              for p in _config.tradeable_instruments],
            return_exceptions=True,
        )
        return [r for r in results if r is not None and not isinstance(r, Exception)]

    signals = asyncio.run(_gather())

    if not signals:
        return {"signals": [], "message": "No snapshots available. Run 'run tech' first."}

    output = []
    for sig in signals:
        output.append({
            "pair": sig.pair,
            "action": sig.action,
            "predicted_direction": sig.predicted_direction,
            "combined_score": round(sig.combined_score, 4),
            "confidence": round(sig.confidence, 4),
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "position_size": sig.position_size,
            "signal_reason": sig.signal_reason,
            "news_score": round(sig.news.sentiment_score, 4),
            "news_confidence": round(sig.news.confidence, 4),
            "price_score": round(sig.price.bias_score, 4),
            "price_confidence": round(sig.price.confidence, 4),
            "generated_at": sig.generated_at.isoformat(),
        })

    return {"signals": output}


# ── POST /close/{pair} ───────────────────────────────────────────

@app.post("/close/{pair}", dependencies=[Depends(_verify_api_key)])
def close_position(pair: str) -> dict[str, Any]:
    """ポジションを緊急決済する。"""
    assert _config is not None and _job_lock is not None

    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance)
    account = pm.get_account_state()

    pos = next(
        (p for p in account.open_positions if p.pair.upper() == pair.upper()),
        None,
    )
    if pos is None:
        open_pairs = [p.pair for p in account.open_positions]
        raise HTTPException(
            status_code=404,
            detail=f"Position not found: {pair}. Open: {open_pairs}",
        )

    try:
        current = fetch_current_price(pos.pair)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Price fetch failed: {e}")

    with _job_lock:
        closed = pm.close_position(pos.order_id, current, "manual")

    if closed is None:
        raise HTTPException(status_code=500, detail="Close failed")

    # 非同期通知（ベストエフォート）
    if _config.notifier.notify_on_order_close:
        try:
            notifier = create_notifier(_config.notifier.notifier)
            asyncio.run(notifier.notify_order_closed(OrderClosedEvent(
                pair=closed.pair,
                direction=closed.direction,
                entry_price=closed.entry_price,
                close_price=current,
                realized_pnl=closed.realized_pnl or 0.0,
                close_reason="manual",
                balance=pm.get_account_state().balance,
            )))
        except Exception as e:
            logger.warning(f"[API] Close notification failed: {e}")

    return {
        "closed": True,
        "pair": closed.pair,
        "direction": closed.direction,
        "entry_price": closed.entry_price,
        "close_price": current,
        "realized_pnl": round(closed.realized_pnl or 0.0, 2),
        "balance": round(pm.get_account_state().balance, 2),
    }


# ── サーバー起動 ─────────────────────────────────────────────────

# uvicorn のログ設定:
#   uvicorn / uvicorn.error は WARNING のみ（起動ノイズを抑制）
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
    job_lock: threading.Lock,
) -> threading.Thread:
    """バックグラウンドスレッドで uvicorn を起動する。"""
    global _config, _store, _analysis_store, _job_lock, _started_at
    _config = config
    _store = store
    _analysis_store = analysis_store
    _job_lock = job_lock
    _started_at = datetime.now()

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
