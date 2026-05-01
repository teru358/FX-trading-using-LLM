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
    """プロセス死活確認 + サブシステム状態 (スケジューラ / LLM CB / TV / snapshot freshness)。

    障害時のトリアージを一発で行えるよう、依存サブシステムの状態を集約する。
    """
    import schedule as sched_mod

    now = db_now()
    jobs = sched_mod.get_jobs()
    next_run = min((j.next_run for j in jobs), default=None) if jobs else None

    # 経過時間
    uptime_seconds: float | None = None
    if state.started_at is not None:
        uptime_seconds = (now - state.started_at).total_seconds()

    # LLM サーキットブレーカー状態
    cb_states: dict[str, dict[str, Any]] = {}
    try:
        from src.llm.client import _circuit_breakers
        for provider, cb in _circuit_breakers.items():
            cb_states[provider] = {
                "state":                cb.state,
                "consecutive_failures": cb._consecutive_failures,
            }
    except Exception:
        pass

    # TV CDP 接続状態
    tv_status: dict[str, Any] = {"enabled": False}
    if state.config is not None and getattr(state.config.tradingview, "enabled", False):
        tv_status["enabled"] = True
        try:
            from src.tradingview.cdp_client import _SHARED_CLIENTS
            clients = list(_SHARED_CLIENTS.values())
            tv_status["clients"] = [
                {"host": c._host, "port": c._port, "connected": c.is_connected}
                for c in clients
            ]
        except Exception:
            tv_status["clients"] = []

    # price_provider 状態
    price_provider_status: str | None = None
    try:
        if state.config is not None:
            from src.data.price_provider import PriceProvider
            pp = PriceProvider(state.config)
            price_provider_status = pp.status_line()
    except Exception as e:
        price_provider_status = f"error: {e}"

    # トレード銘柄の最新スナップショット時刻 (=テクニカル収集の生存確認)
    snapshots_status: list[dict[str, Any]] = []
    if state.config is not None and state.analysis_store is not None:
        for inst in getattr(state.config, "tradeable_instruments", []):
            try:
                snaps = state.analysis_store.get_recent_snapshots(inst.symbol, hours=24)
                if snaps:
                    latest_at = snaps[0].analyzed_at
                    age_minutes = (now - latest_at).total_seconds() / 60.0
                    snapshots_status.append({
                        "symbol":       inst.symbol,
                        "latest_at":    latest_at.isoformat(),
                        "age_minutes":  round(age_minutes, 1),
                    })
                else:
                    snapshots_status.append({
                        "symbol":       inst.symbol,
                        "latest_at":    None,
                        "age_minutes":  None,
                    })
            except Exception as e:
                snapshots_status.append({"symbol": inst.symbol, "error": str(e)})

    # 取引モード
    trading_mode = state.config.trading.trading_mode if state.config else None

    return {
        "status": "ok",
        "trading_mode":   trading_mode,
        "started_at":     state.started_at.isoformat() if state.started_at else None,
        "uptime_seconds": uptime_seconds,
        "now":            now.isoformat(),
        "scheduler": {
            "jobs_count": len(jobs),
            "next_run":   next_run.isoformat() if next_run else None,
        },
        "llm_circuit_breakers": cb_states,
        "tradingview":          tv_status,
        "price_provider":       price_provider_status,
        "snapshots":            snapshots_status,
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


@router.get("/usage", dependencies=[Depends(verify_api_key)])
def usage() -> dict[str, Any]:
    """LLM プロバイダ別の使用量 / サーキットブレーカー詳細。

    /health でも一部返すが、こちらはエラー履歴や usage_limit ヒット数など
    より詳しい情報を返す。クライアント (`usage` コマンド) から参照する。

    claude_in_use=False のときは Claude 系プロバイダが設定されておらず
    usage_limit 追跡対象がないことを示す。クライアント側で非表示・警告切替。
    """
    # Claude 系プロバイダが設定されているか判定 (claude-cli / claude)
    # 新スキーマでは llm.provider が単一 → 全 role が同じ provider を共有
    claude_providers = {"claude-cli", "claude"}
    claude_roles: list[str] = []
    if state.config is not None and state.config.llm.provider in claude_providers:
        claude_roles = list(("news_analysis", "price_analysis", "reflection"))

    providers: dict[str, Any] = {}
    try:
        from src.llm.client import _circuit_breakers
        for provider, cb in _circuit_breakers.items():
            providers[provider] = cb.snapshot()
    except Exception as e:
        return {"error": str(e), "providers": {}}

    return {
        "now":           db_now().isoformat(),
        "claude_in_use": bool(claude_roles),
        "claude_roles":  claude_roles,
        "providers":     providers,
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

    def _resolve_job_name(job_func: Any) -> str:
        """schedule の job_func から本来の対象関数名を解決する。

        main.py で ``_run_with_guard`` / ``_run_with_slot`` ラッパーを噛ませて
        登録しているため ``job_func`` は ``functools.partial`` となり、その
        ``__name__`` は schedule ライブラリがラッパー名 (例: "_run_with_slot")
        を付けてしまう。partial の場合は ``args`` 内から最初の
        "callable かつ __name__ を持つもの" を対象関数とみなす
        (ラッパー設計上 guard インスタンスは __name__ なしで除外される)。
        """
        # partial 経由のラッパー登録: args から target fn を探す
        for a in getattr(job_func, "args", ()):
            if callable(a) and hasattr(a, "__name__"):
                return a.__name__
        # 直接登録: __name__ をそのまま使う
        if hasattr(job_func, "__name__"):
            return job_func.__name__
        wrapped = getattr(job_func, "func", None)
        return getattr(wrapped, "__name__", str(job_func))

    jobs = sched_mod.get_jobs()

    # カテゴリ別に分類
    categorized: dict[str, list[dict]] = {}
    for j in jobs:
        name = _resolve_job_name(j.job_func)
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
