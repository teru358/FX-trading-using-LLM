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
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from src.config import AppConfig, BASE_DIR
from src.data.analysis_store import AnalysisStore
from src.data.price_fetcher import fetch_current_price
from src.data.price_store import PriceStore
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
_price_store: PriceStore | None = None
_hold_store = None        # HoldDecisionStore
_forecast_store = None    # ForecastStore

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
    pm = PositionManager(state_store, _config.trading.initial_balance, context="API_Status")
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
            current = fetch_current_price(pos.pair).price
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


# ── GET /news ────────────────────────────────────────────────────

@app.get("/news", dependencies=[Depends(_verify_api_key)])
def news() -> dict[str, Any]:
    """カテゴリ別の最新ニュースセンチメント（run news と同等）。"""
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

    return {"lookback_hours": _config.rag.news_lookback_hours, "categories": result}


# ── GET /tech ────────────────────────────────────────────────────

@app.get("/tech", dependencies=[Depends(_verify_api_key)])
def tech() -> dict[str, Any]:
    """銘柄別の最新テクニカルスナップショット（run tech と同等）。"""
    assert _config is not None and _analysis_store is not None

    all_instruments = _config.watch_only_instruments + _config.tradeable_instruments
    snapshots = []
    for inst in all_instruments:
        snaps = _analysis_store.get_recent_snapshots(
            inst.symbol, hours=_config.rag.analysis_lookback_hours
        )
        if snaps:
            s = snaps[0]
            snapshots.append({
                "symbol":            s.symbol,
                "display_name":      inst.display_name,
                "analyzed_at":       s.analyzed_at.isoformat() if s.analyzed_at else None,
                "direction_bias":    s.direction_bias,
                "bias_score":        s.bias_score,
                "confidence":        s.confidence,
                "entry_zone_low":    s.entry_zone_low,
                "entry_zone_high":   s.entry_zone_high,
                "stop_loss":         s.stop_loss,
                "take_profit":       s.take_profit,
                "risk_reward_ratio": s.risk_reward_ratio,
                "reasoning_summary": s.reasoning_summary,
            })
        else:
            snapshots.append({
                "symbol":       inst.symbol,
                "display_name": inst.display_name,
                "analyzed_at":  None,
            })

    return {"lookback_hours": _config.rag.analysis_lookback_hours, "snapshots": snapshots}


# ── GET /analyze ─────────────────────────────────────────────────

@app.get("/analyze", dependencies=[Depends(_verify_api_key)])
async def analyze() -> dict[str, Any]:
    """保存済みスナップショット＋ニュースから総合シグナルを返す（新規LLM取得なし）。"""
    assert _config is not None and _store is not None and _analysis_store is not None

    from src.trading_cycle import _summarize_pair

    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance, context="API_Analyze")

    results = await asyncio.gather(
        *[_summarize_pair(p, _config, pm, _store, _analysis_store)
          for p in _config.tradeable_instruments],
        return_exceptions=True,
    )
    signals = [r for r in results if r is not None and not isinstance(r, Exception)]

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


# ── GET /forecast ─────────────────────────────────────────────────

@app.get("/forecast", dependencies=[Depends(_verify_api_key)])
def forecast(pair: str | None = None, hours: int = 24) -> dict[str, Any]:
    """直近N時間の予測サイクルレコードを返す（skip含む全レコード）。"""
    assert _config is not None and _forecast_store is not None

    instruments = _config.tradeable_instruments
    if pair:
        instruments = [i for i in instruments if i.symbol.upper() == pair.upper()]

    result: dict[str, Any] = {}
    for inst in instruments:
        records = _forecast_store.get_recent_all(inst.symbol, hours=hours)
        result[inst.symbol] = [
            {
                "id":                  r.id,
                "forecast_ts":         r.forecast_ts.isoformat(),
                "current_price":       r.current_price,
                "predicted_direction": r.predicted_direction,
                "combined_score":      r.combined_score,
                "confidence":          r.confidence,
                "signal_reason":       r.signal_reason,
                "reviewed":            r.reviewed,
                "latest_review_ts":    r.latest_review_ts.isoformat() if r.latest_review_ts else None,
                "latest_price_delta":  r.latest_price_delta,
            }
            for r in records
        ]

    return {"hours": hours, "forecasts": result}


# ── GET /feeds ────────────────────────────────────────────────────

@app.get("/feeds", dependencies=[Depends(_verify_api_key)])
def feeds() -> dict[str, Any]:
    """RSSフィード疎通確認（カテゴリ別・フィード別の状態を返す）。"""
    import feedparser
    from src.analysis.rss_fetcher import feed_short_name

    assert _config is not None

    global_kw = frozenset(k.lower() for k in _config.keywords.global_keywords)
    japan_kw = frozenset(k.lower() for k in _config.keywords.japan_keywords)

    categories_def = [
        ("FX",     _config.news_sources.feeds_fx,     None),
        ("Global", _config.news_sources.feeds_global, global_kw),
        ("Japan",  _config.news_sources.feeds_japan,  japan_kw),
    ]

    result: dict[str, Any] = {}
    for label, feed_urls, keywords in categories_def:
        feed_results = []
        feeds_ok = 0
        filtered_count = 0

        for feed_url in feed_urls:
            name = feed_short_name(feed_url)
            try:
                parsed = feedparser.parse(feed_url)
                count = len(parsed.entries)
                latest_title = parsed.entries[0].get("title", "") if parsed.entries else ""
                status = "ok" if count > 0 else "empty"
                feeds_ok += 1

                # Count keyword-filtered entries (simplified: check title+summary contains keyword)
                if keywords is None:
                    # FX: no keyword filter, count up to 5 recent entries
                    filtered_count += min(count, 5)
                else:
                    for entry in parsed.entries[:20]:
                        title = entry.get("title", "")
                        body = entry.get("summary", "")[:600]
                        combined = (title + " " + body).lower()
                        if any(kw in combined for kw in keywords):
                            filtered_count += 1

            except Exception:
                count = 0
                latest_title = ""
                status = "failed"

            feed_results.append({
                "name": name,
                "status": status,
                "count": count,
                "latest_title": latest_title[:80],
            })

        result[label] = {
            "feeds": feed_results,
            "filtered_count": filtered_count,
            "feeds_ok": feeds_ok,
            "total_feeds": len(feed_urls),
        }

    return {"categories": result}


# ── GET /logs ─────────────────────────────────────────────────────

_LOG_LINES_MAX = 500

@app.get("/logs", dependencies=[Depends(_verify_api_key)])
def logs(lines: int = 100) -> dict[str, Any]:
    """activity.log の末尾N行を返す（最大500行）。"""
    assert _config is not None

    lines = min(max(1, lines), _LOG_LINES_MAX)
    log_path: Path = BASE_DIR / _config.logging.activity_log_file

    if not log_path.exists():
        return {"lines": [], "total_lines": 0, "returned": 0}

    with open(log_path, encoding="utf-8") as f:
        all_lines = f.readlines()

    tail = all_lines[-lines:]
    return {
        "lines":       [l.rstrip("\n") for l in tail],
        "total_lines": len(all_lines),
        "returned":    len(tail),
    }


# ── GET /schedule ─────────────────────────────────────────────────

@app.get("/schedule", dependencies=[Depends(_verify_api_key)])
def schedule_info() -> dict[str, Any]:
    """スケジュール情報を返す。

    - 取引サイクル・予測サイクルの次回実行時刻
    - 各サイクルの全スケジュール
    - ニュース取得間隔
    - price_monitor (5分間隔) は除外
    """
    import schedule as sched_mod
    from datetime import datetime

    # ジョブ名 → カテゴリ分類
    _CATEGORY = {
        "run_trading_cycle":      "trading",
        "run_forecast_cycle":     "forecast",
        "run_news_collection":    "news",
        "run_technical_collection": "technical",
        "run_exit_check_cycle":   "exit_check",
    }
    _HIDDEN = {"run_price_monitor", "_run_rag_cleanup"}

    jobs = sched_mod.get_jobs()

    # カテゴリ別に分類
    categorized: dict[str, list[dict]] = {}
    for j in jobs:
        name = getattr(j.job_func, "__name__", str(j.job_func))
        if name in _HIDDEN:
            continue
        cat = _CATEGORY.get(name, "other")
        entry = {
            "time": j.next_run.strftime("%H:%M") if j.next_run else None,
            "next_run": j.next_run.isoformat() if j.next_run else None,
            "last_run": j.last_run.isoformat() if j.last_run else None,
        }
        categorized.setdefault(cat, []).append(entry)

    def _next_for(cat: str) -> str | None:
        entries = categorized.get(cat, [])
        times = [e["next_run"] for e in entries if e["next_run"]]
        return min(times) if times else None

    def _all_times(cat: str) -> list[str]:
        entries = categorized.get(cat, [])
        return sorted({e["time"] for e in entries if e["time"]})

    return {
        "trading": {
            "next_run": _next_for("trading"),
            "schedule": _all_times("trading"),
        },
        "forecast": {
            "next_run": _next_for("forecast"),
            "schedule": _all_times("forecast"),
        },
        "news": {
            "next_run": _next_for("news"),
            "schedule": _all_times("news"),
        },
        "technical": {
            "next_run": _next_for("technical"),
            "schedule": _all_times("technical"),
        },
        "exit_check": {
            "next_run": _next_for("exit_check"),
            "schedule": _all_times("exit_check"),
        },
    }


# ── POST /run/trade ───────────────────────────────────────────────

@app.post("/run/trade", dependencies=[Depends(_verify_api_key)])
def run_trade() -> dict[str, Any]:
    """取引判定ループを手動実行する（同期・job_lock取得）。実行後の最新状態を返す。"""
    assert _config is not None and _job_lock is not None
    assert _store is not None and _analysis_store is not None
    assert _price_store is not None and _hold_store is not None

    from src.trading_cycle import run_trading_cycle as _run_trading_cycle

    started_at = datetime.now()
    with _job_lock:
        _run_trading_cycle(_config, _store, _price_store, _analysis_store, _hold_store)
    elapsed = (datetime.now() - started_at).total_seconds()

    # 実行後の最新状態を取得
    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance, context="API_RunTrade")
    account = pm.get_account_state()

    positions = []
    for pos in account.open_positions:
        entry: dict[str, Any] = {
            "pair":          pos.pair,
            "direction":     pos.direction,
            "entry_price":   pos.entry_price,
            "stop_loss":     pos.stop_loss,
            "take_profit":   pos.take_profit,
            "position_size": pos.position_size,
            "opened_at":     pos.opened_at.isoformat(),
        }
        try:
            current = fetch_current_price(pos.pair).price
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
        "status":          "completed",
        "elapsed_seconds": round(elapsed, 1),
        "executed_at":     started_at.isoformat(),
        "balance":         account.balance,
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl / account.initial_balance * 100, 2),
        "total_trades":    account.total_trades,
        "open_positions":  positions,
    }


# ── POST /run/news （コメントアウト：将来有効化用） ──────────────
#
# @app.post("/run/news", dependencies=[Depends(_verify_api_key)])
# def run_news() -> dict[str, Any]:
#     """ニュース収集を手動実行する（同期・job_lock取得）。"""
#     assert _config is not None and _job_lock is not None and _store is not None
#
#     from src.jobs.news_collector import run_news_collection
#
#     started_at = datetime.now()
#     with _job_lock:
#         run_news_collection(_config, _store)
#     elapsed = (datetime.now() - started_at).total_seconds()
#
#     return {"status": "completed", "elapsed_seconds": round(elapsed, 1),
#             "executed_at": started_at.isoformat()}


# ── POST /run/tech （コメントアウト：将来有効化用） ──────────────
#
# @app.post("/run/tech", dependencies=[Depends(_verify_api_key)])
# def run_tech() -> dict[str, Any]:
#     """テクニカル収集を手動実行する（同期・job_lock取得）。"""
#     assert _config is not None and _job_lock is not None
#     assert _store is not None and _price_store is not None and _analysis_store is not None
#
#     from src.jobs.technical_collector import run_technical_collection
#
#     started_at = datetime.now()
#     with _job_lock:
#         run_technical_collection(_config, _store, _price_store, _analysis_store, force=True)
#     elapsed = (datetime.now() - started_at).total_seconds()
#
#     return {"status": "completed", "elapsed_seconds": round(elapsed, 1),
#             "executed_at": started_at.isoformat()}


# ── POST /ask ─────────────────────────────────────────────────────

class _AskRequest(BaseModel):
    message: str


@app.post("/ask", dependencies=[Depends(_verify_api_key)])
def ask(body: _AskRequest) -> dict[str, Any]:
    """FX分析LLMへ質問する（同期・LLM呼び出しあり）。UIはローディング表示を推奨。"""
    assert _config is not None and _store is not None and _analysis_store is not None

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    from src.trading_cycle import run_ask as _run_ask

    started_at = datetime.now()
    try:
        response = _run_ask(message, _config, _store, _analysis_store)
    except Exception as e:
        logger.error(f"[API] /ask failed: {e}", exc_info=True)
        raise HTTPException(status_code=504, detail=f"LLM error: {e}")
    elapsed = (datetime.now() - started_at).total_seconds()

    return {
        "message":         message,
        "response":        response,
        "elapsed_seconds": round(elapsed, 1),
    }


# ── POST /close/{pair} ───────────────────────────────────────────

@app.post("/close/{pair}", dependencies=[Depends(_verify_api_key)])
async def close_position(pair: str) -> dict[str, Any]:
    """ポジションを緊急決済する。"""
    assert _config is not None and _job_lock is not None

    state_store = StateStore(_config.state_dir)
    pm = PositionManager(state_store, _config.trading.initial_balance, context="API_Close")
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
        current = fetch_current_price(pos.pair).price
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
            await notifier.notify_order_closed(OrderClosedEvent(
                pair=closed.pair,
                direction=closed.direction,
                entry_price=closed.entry_price,
                close_price=current,
                realized_pnl=closed.realized_pnl or 0.0,
                close_reason="manual",
                balance=pm.get_account_state().balance,
                source="manual",
            ))
        except Exception as e:
            logger.warning(f"[API] Close notification failed: {e}")

    return {
        "closed":       True,
        "pair":         closed.pair,
        "direction":    closed.direction,
        "entry_price":  closed.entry_price,
        "close_price":  current,
        "realized_pnl": round(closed.realized_pnl or 0.0, 2),
        "balance":      round(pm.get_account_state().balance, 2),
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
    price_store: PriceStore,
    hold_store,        # HoldDecisionStore
    forecast_store,    # ForecastStore
) -> threading.Thread:
    """バックグラウンドスレッドで uvicorn を起動する。"""
    global _config, _store, _analysis_store, _job_lock, _started_at
    global _price_store, _hold_store, _forecast_store
    _config = config
    _store = store
    _analysis_store = analysis_store
    _job_lock = job_lock
    _price_store = price_store
    _hold_store = hold_store
    _forecast_store = forecast_store
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
