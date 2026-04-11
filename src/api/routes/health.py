"""ヘルスチェック / システム状態系のエンドポイント。

GET /health     — プロセス死活確認 + スケジューラ状態
GET /status     — 残高・ポジション一覧
GET /logs       — activity.log の末尾 N 行
GET /schedule   — スケジュール情報 (取引/予測/ニュース/技術/exit_check)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from src.api._state import state, verify_api_key
from src.config import BASE_DIR
from src.data.price_fetcher import fetch_current_price
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager
from src.utils.clock import db_now

router = APIRouter()


@router.get("/health", dependencies=[Depends(verify_api_key)])
def health() -> dict[str, Any]:
    """プロセス死活確認 + スケジューラ状態。"""
    import schedule as sched_mod

    jobs = sched_mod.get_jobs()
    next_run = min((j.next_run for j in jobs), default=None) if jobs else None

    return {
        "status": "ok",
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "now": db_now().isoformat(),
        "scheduler": {
            "jobs_count": len(jobs),
            "next_run": next_run.isoformat() if next_run else None,
        },
    }


@router.get("/status", dependencies=[Depends(verify_api_key)])
def status() -> dict[str, Any]:
    """残高・ポジション一覧。"""
    assert state.config is not None
    state_store = StateStore(state.config.state_dir)
    pm = PositionManager(state_store, state.config.trading.initial_balance, context="API_Status")
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


_LOG_LINES_MAX = 500


@router.get("/logs", dependencies=[Depends(verify_api_key)])
def logs(lines: int = 100) -> dict[str, Any]:
    """activity.log の末尾 N 行を返す (最大 500 行)。"""
    assert state.config is not None

    lines = min(max(1, lines), _LOG_LINES_MAX)
    log_path: Path = BASE_DIR / state.config.logging.activity_log_file

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


@router.get("/schedule", dependencies=[Depends(verify_api_key)])
def schedule_info() -> dict[str, Any]:
    """スケジュール情報を返す。

    - 取引サイクル・予測サイクルの次回実行時刻
    - 各サイクルの全スケジュール
    - ニュース取得間隔
    - price_monitor (5分間隔) は除外
    """
    import schedule as sched_mod

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
