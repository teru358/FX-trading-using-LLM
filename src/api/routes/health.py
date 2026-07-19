"""ヘルスチェック / システム状態系のエンドポイント。

GET /status     — 運用状態レポート (プロセス + halt + サブシステム健全性)
GET /logs       — activity.log の末尾 N 行
GET /usage      — LLM 使用量
GET /schedule   — スケジュール情報

役割の境界:
- /status は「Bot が今、新規発注を受け付ける状態か」を 1 ヶ所で確認するための応答。
  プロセス基本情報 (mode/uptime/scheduler) と halt と全サブシステム健全性をまとめる。
- /account は残高・ポジション (取引固有情報) のみ。halt は /status へ集約。

注意:
- `halt` は finance 側 (halt.json) の権威的な状態。
- `mt5_bridge.soft_halted` / `is_hard_halted` / `accepts_new_orders` は bridge プロセス
  側の状態で finance halt とは別物。
- 旧 /health (軽量 liveness) は本 endpoint に統合され削除された。MT5 bridge プロセス
  側の /health (mt5_bridge/server.py) は別 endpoint で継続使用される。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from src.api._state import state, verify_api_key
from src.config import BASE_DIR
from src.utils.clock import db_now

router = APIRouter()


@router.get("/status", dependencies=[Depends(verify_api_key)])
def status() -> dict[str, Any]:
    """運用状態レポート (プロセス + halt + サブシステム健全性の統合応答)。

    旧 /health のフィールド (status/mode/live_broker/started_at/uptime/scheduler) と、
    finance 側 halt (halt.json)、LLM CB、price_provider、snapshots、MT5 bridge 状態
    を 1 ヶ所にまとめる。bridge 不通でも本 endpoint は 200 を返す (`mt5_bridge.reachable`
    で判定)。
    """
    import schedule as sched_mod

    now = db_now()
    jobs = sched_mod.get_jobs()
    next_run = min((j.next_run for j in jobs), default=None) if jobs else None

    uptime_seconds: float | None = None
    if state.started_at is not None:
        uptime_seconds = (now - state.started_at).total_seconds()

    mode = state.config.mode if state.config else None
    live_broker = state.config.live_broker if state.config else None

    # finance 側 halt 状態 (halt.json — paper モードでは default)
    halt_section: dict[str, Any]
    if state.config is not None:
        from src.persistence import halt_state
        halt = halt_state.read(state.config.state_dir)
        halt_section = {
            "soft_halted":                halt.soft_halted,
            "auto_triggered":             halt.auto_triggered,
            "reason":                     halt.reason,
            "since":                      halt.since,
            "triggered_by":               halt.triggered_by,
            # 導出: 「halt = 全停止」ではなく「新規発注停止、既存ポジション管理は継続」
            "blocks_new_orders":          halt.soft_halted,
            "allows_position_management": True,
        }
    else:
        halt_section = {
            "soft_halted":                False,
            "auto_triggered":             False,
            "reason":                     "",
            "since":                      None,
            "triggered_by":               "",
            "blocks_new_orders":          False,
            "allows_position_management": True,
        }

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

    # price_provider 状態
    price_provider_status: str | None = None
    try:
        if state.config is not None:
            from src.data.price_provider import PriceProvider
            pp = PriceProvider(state.config)
            price_provider_status = pp.status_line()
    except Exception as e:
        price_provider_status = f"error: {e}"

    # トレード銘柄の最新スナップショット時刻
    snapshots_status: list[dict[str, Any]] = []
    if state.config is not None and state.analysis_store is not None:
        for inst in getattr(state.config, "tradeable_instruments", []):
            try:
                snaps = state.analysis_store.get_recent_ok_snapshots(inst.symbol, hours=24)
                if snaps:
                    latest_at = snaps[0].analyzed_at
                    age_minutes = (now - latest_at).total_seconds() / 60.0
                    snapshots_status.append({
                        "symbol":      inst.symbol,
                        "latest_at":   latest_at.isoformat(),
                        "age_minutes": round(age_minutes, 1),
                    })
                else:
                    snapshots_status.append({
                        "symbol":      inst.symbol,
                        "latest_at":   None,
                        "age_minutes": None,
                    })
            except Exception as e:
                snapshots_status.append({"symbol": inst.symbol, "error": str(e)})

    from src.jobs.econ_impact_job import get_econ_impact_status
    econ_status = get_econ_impact_status()

    return {
        "status":               "ok",
        "mode":                 mode,
        "live_broker":          live_broker,
        "started_at":           state.started_at.isoformat() if state.started_at else None,
        "uptime_seconds":       uptime_seconds,
        "now":                  now.isoformat(),
        "scheduler": {
            "jobs_count": len(jobs),
            "next_run":   next_run.isoformat() if next_run else None,
        },
        "halt":                 halt_section,
        "llm_circuit_breakers": cb_states,
        "price_provider":       price_provider_status,
        "snapshots":            snapshots_status,
        "mt5_bridge":           _get_mt5_bridge_status(),
        "econ_impact_last_attempt": econ_status["last_attempt"],
        "econ_impact_last_success": econ_status["last_success"],
        "econ_impact_pending":      econ_status["pending"],
        "econ_impact_failed":       econ_status["failed"],
    }


def _get_mt5_bridge_status() -> dict[str, Any]:
    """bridge /health + /admin/status をプロキシして MT5 ブリッジ状態を返す。

    タイムアウトは 2 秒、失敗時は reachable: false で error 内容を返す。
    finance 側 reconciliation_state.json の skip 連続回数も同梱する
    (bridge 不通時こそ確認したい指標なので、失敗 return にも必ず含める)。
    """
    cfg = state.config.providers.mt5 if state.config else None
    if cfg is None or not cfg.bridge_url:
        return {"configured": False}

    # 永続カウンタ読み出し (broker インスタンス非依存)
    skip_consecutive: int | None = None
    if state.config is not None:
        try:
            from src.persistence import reconciliation_state
            skip_consecutive = reconciliation_state.read(
                state.config.state_dir,
            ).skipped_consecutive
        except Exception:  # noqa: BLE001
            skip_consecutive = None

    try:
        import httpx
        url = cfg.bridge_url.rstrip("/")
        h = httpx.get(f"{url}/health", timeout=2.0)
        admin_headers = (
            {"X-Bridge-Api-Key": cfg.api_key}
            if getattr(cfg, "api_key", "") else {}
        )
        admin = httpx.get(
            f"{url}/admin/status", timeout=2.0, headers=admin_headers,
        )
    except Exception as e:  # noqa: BLE001
        # 不通時こそ skip 連続回数を見せる
        return {
            "configured":                          True,
            "reachable":                           False,
            "error":                               f"{type(e).__name__}: {e}",
            "reconciliation_skipped_consecutive":  skip_consecutive,
        }

    health_ok = h.is_success
    health_body = h.json() if health_ok else {}
    admin_body = admin.json() if admin.is_success else {}

    return {
        "configured":                          True,
        "reachable":                           health_ok,
        "mt5_connected":                       health_body.get("mt5_connected", False),
        "dry_run":                             admin_body.get("dry_run"),
        "soft_halted":                         admin_body.get("soft_halted"),
        "is_hard_halted":                      admin_body.get("is_hard_halted"),
        "accepts_new_orders":                  admin_body.get("accepts_new_orders"),
        "reconciliation_skipped_consecutive":  skip_consecutive,
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

    /status でも CB の state / consecutive_failures は返るが、こちらはエラー履歴や
    usage_limit ヒット数などより詳しい情報を返す。クライアント (`usage` コマンド)
    から参照する。

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

    - 取引サイクルの次回実行時刻
    - 各サイクルの全スケジュール
    - ニュース取得間隔
    - price_monitor (5分間隔) は除外
    """
    import schedule as sched_mod

    # ジョブ名 → カテゴリ分類
    _CATEGORY = {
        "run_trading_cycle":      "trading",
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
