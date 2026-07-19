"""FX Trading Bot — 対話型クライアント。

REST API 経由でデーモンプロセス (ローカル or リモート) を操作する。
バックグラウンドスレッドで activity.log をポーリングし、
新着ログをリアルタイムに表示する。

使い方:
    uv run python client.py                       # 対話 (default ホスト)
    uv run python client.py --host remote         # 対話 (remote プロファイル)
    uv run python client.py --url http://x:8811   # 対話 (URL 直接指定)
    uv run python client.py status                # 1 コマンド実行
    uv run python client.py --host main logs 50   # 指定ホストで logs

ホストプロファイルは config/hosts.yaml (gitignored、例: hosts.yaml.example) で定義。
未配置時は FINANCE_HOST 環境変数 + settings.yaml のポートで組み立てる (従来動作)。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from prompt_toolkit import prompt as pt_prompt
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich import box

# ── 設定読み込み ─────────────────────────────────────────────────

_BASE_DIR = Path(__file__).parent
load_dotenv(_BASE_DIR / ".env")
_console = Console()


@dataclass
class HostProfile:
    """1 ホスト分の接続設定。"""
    name: str
    url: str
    api_key: str = ""


@dataclass
class ConnectionState:
    """クライアントの現在の接続状態 (ミュータブル・実行中切替可)。"""
    profile: HostProfile
    # ログポーラー用の接続状態フラグ (False = 直近失敗)
    online: bool = True
    last_poll_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.profile.api_key} if self.profile.api_key else {}


def _load_hosts_config() -> tuple[dict[str, HostProfile], str | None]:
    """config/hosts.yaml から全ホストプロファイルと default 名を取得。

    Returns:
        (profiles_by_name, default_name)  未配置時は ({}, None)。
    """
    cfg_path = _BASE_DIR / "config" / "hosts.yaml"
    if not cfg_path.exists():
        return {}, None
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        _console.print(f"[red]hosts.yaml 読み込みエラー: {e}[/red]")
        return {}, None

    profiles: dict[str, HostProfile] = {}
    for name, spec in (cfg.get("hosts") or {}).items():
        url = (spec or {}).get("url", "").rstrip("/")
        if not url:
            continue
        env_name = (spec or {}).get("api_key_env", "API_SECRET_KEY")
        api_key = os.environ.get(env_name, "")
        profiles[name] = HostProfile(name=name, url=url, api_key=api_key)
    default = cfg.get("default")
    return profiles, default


def _build_legacy_profile() -> HostProfile:
    """hosts.yaml が無い場合の従来動作: FINANCE_HOST + settings.yaml。"""
    cfg_path = _BASE_DIR / "config" / "settings.yaml"
    port = 8811
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            port = cfg.get("api", {}).get("port", 8811)
        except Exception:
            pass
    api_key = os.environ.get("API_SECRET_KEY", "")
    host = os.environ.get("FINANCE_HOST", f"localhost:{port}")
    return HostProfile(name="default", url=f"http://{host}", api_key=api_key)


def _resolve_initial_profile(
    host_arg: str | None, url_arg: str | None,
) -> tuple[HostProfile, dict[str, HostProfile]]:
    """CLI 引数と hosts.yaml から初期プロファイルを決定。

    優先順位:
      1. --url があれば ad-hoc プロファイル
      2. --host があれば hosts.yaml から参照
      3. hosts.yaml の default
      4. FINANCE_HOST 環境変数 + settings.yaml
    """
    profiles, default_name = _load_hosts_config()

    if url_arg:
        api_key = os.environ.get("API_SECRET_KEY", "")
        ad_hoc = HostProfile(name="(url)", url=url_arg.rstrip("/"), api_key=api_key)
        return ad_hoc, profiles

    if host_arg:
        if host_arg not in profiles:
            _console.print(
                f"[red]host {host_arg!r} not in config/hosts.yaml[/red]  "
                f"(available: {', '.join(profiles) or '(none)'})"
            )
            sys.exit(2)
        return profiles[host_arg], profiles

    if default_name and default_name in profiles:
        return profiles[default_name], profiles

    # fallback: legacy FINANCE_HOST 動作
    return _build_legacy_profile(), profiles


# グローバル接続状態 (main() で初期化)
_conn: ConnectionState = None  # type: ignore[assignment]
_profiles: dict[str, HostProfile] = {}


# ── HTTP ヘルパー ────────────────────────────────────────────────

def _parse_host_port(url: str) -> tuple[str, int]:
    """URL からホスト名とポートを抽出 (診断用)。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _describe_error(url: str, e: Exception) -> str:
    """HTTP エラーを人間可読な診断メッセージに変換する。

    ポート開放・DNS 解決・デーモン起動状態など、原因の切り分けを助ける。
    """
    host, port = _parse_host_port(url)
    err_type = type(e).__name__
    msg = str(e).lower()

    # 1. タイムアウト系
    if isinstance(e, httpx.ConnectTimeout):
        return (
            f"[red]接続タイムアウト[/red] ([cyan]{host}:{port}[/cyan] に TCP 接続できず)\n"
            f"  [dim]可能性:[/dim] ファイアウォール/ポート未開放 / ホスト down / ネットワーク経路断\n"
            f"  [dim]確認:[/dim] [cyan]ping {host}[/cyan] / "
            f"[cyan]nc -vz {host} {port}[/cyan] / "
            f"サーバ側で [cyan]ss -tlnp | grep {port}[/cyan]"
        )
    if isinstance(e, httpx.ReadTimeout):
        return (
            f"[yellow]読み取りタイムアウト[/yellow] ([cyan]{host}:{port}[/cyan] は接続したが応答遅延)\n"
            f"  [dim]可能性:[/dim] LLM 呼び出しが長引いている / サーバ側高負荷"
        )
    if isinstance(e, httpx.TimeoutException):
        return (
            f"[red]タイムアウト[/red] ([cyan]{host}:{port}[/cyan]): {e}\n"
            f"  [dim]確認:[/dim] [cyan]nc -vz {host} {port}[/cyan] でポート到達性"
        )

    # 2. 接続エラー系
    if isinstance(e, httpx.ConnectError):
        if "name or service not known" in msg or "nodename nor servname" in msg or "[errno -2]" in msg:
            return (
                f"[red]DNS 解決失敗[/red] (ホスト名 [cyan]{host}[/cyan] が見つからない)\n"
                f"  [dim]確認:[/dim] [cyan]getent hosts {host}[/cyan] / mDNS (.local) なら avahi-daemon が動いているか"
            )
        if "connection refused" in msg:
            return (
                f"[red]接続拒否[/red] ([cyan]{host}:{port}[/cyan] はポートを閉じている)\n"
                f"  [dim]可能性:[/dim] finance デーモン未起動 / 別ポートで稼働中\n"
                f"  [dim]確認:[/dim] サーバ側で "
                f"[cyan]systemctl --user status finance.service[/cyan]"
            )
        if "no route to host" in msg:
            return (
                f"[red]経路なし[/red] ([cyan]{host}[/cyan] にルーティング不可)\n"
                f"  [dim]確認:[/dim] 同一 LAN か / VPN 状態 / [cyan]ip route[/cyan]"
            )
        if "network is unreachable" in msg:
            return (
                f"[red]ネットワーク到達不可[/red]\n"
                f"  [dim]確認:[/dim] [cyan]ip addr[/cyan] でインターフェース状態"
            )
        return f"[red]接続失敗[/red] ({err_type}): {e}"

    # 3. HTTP ステータスエラー
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        body = e.response.text[:200]
        if code == 401 or code == 403:
            return (
                f"[red]認証エラー[/red] (HTTP {code}): {body}\n"
                f"  [dim]確認:[/dim] [cyan]API_SECRET_KEY[/cyan] が両側で一致しているか"
            )
        if code == 404:
            return f"[yellow]エンドポイント未実装[/yellow] (HTTP 404): {url}"
        return f"[red]HTTP {code}[/red]: {body}"

    # 4. その他
    return f"[red]エラー[/red] ({err_type}): {e}"


def _get(path: str, params: dict | None = None) -> dict[str, Any] | None:
    url = f"{_conn.profile.url}{path}"
    try:
        r = httpx.get(url, headers=_conn.headers, params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _console.print(_describe_error(url, e))
        return None


def _post(
    path: str,
    json: dict | None = None,
    timeout: float = 300.0,
) -> dict[str, Any] | None:
    url = f"{_conn.profile.url}{path}"
    try:
        r = httpx.post(url, headers=_conn.headers, json=json, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _console.print(_describe_error(url, e))
        return None


# ── コマンド実装 ─────────────────────────────────────────────────

def _cmd_status() -> None:
    """運用状態レポート: プロセス + halt + サブシステム健全性の統合応答を表示。"""
    data = _get("/status")
    if data is None:
        return

    # プロセス基本情報 (旧 /health から統合)
    mode = data.get("mode") or "-"
    live_broker = data.get("live_broker") or "null"
    started = data.get("started_at", "N/A")
    uptime_s = data.get("uptime_seconds")
    uptime_str = "N/A"
    if uptime_s is not None:
        hours = int(uptime_s // 3600)
        mins = int((uptime_s % 3600) // 60)
        uptime_str = f"{hours}h {mins}m"
    sched = data.get("scheduler") or {}
    jobs = sched.get("jobs_count", "N/A")
    next_run = sched.get("next_run", "N/A")
    _console.print(
        f"\n[green]● online[/green]  host: [cyan]{_conn.profile.name}[/cyan] "
        f"({_conn.profile.url})  "
        f"mode=[cyan]{mode}[/cyan]  live_broker=[cyan]{live_broker}[/cyan]  "
        f"uptime=[cyan]{uptime_str}[/cyan]"
    )
    _console.print(f"  起動: [cyan]{started}[/cyan]")
    _console.print(f"  スケジューラ: {jobs} ジョブ  次回実行: [cyan]{next_run}[/cyan]")

    # halt 状態 (finance 側 halt.json)
    halt = data.get("halt") or {}
    if halt.get("soft_halted"):
        trig = "auto" if halt.get("auto_triggered") else "manual"
        reason = halt.get("reason", "")
        since = halt.get("since", "?")
        _console.print(
            f"[red]⚠ SOFT HALT ({trig})[/red] "
            f"[dim]since {since}: {reason}[/dim]"
        )

    # LLM circuit breakers
    cbs = data.get("llm_circuit_breakers") or {}
    if cbs:
        cb_parts = []
        for provider, st in cbs.items():
            s = st.get("state", "?")
            fails = st.get("consecutive_failures", 0)
            color = "green" if s == "closed" else "red" if s == "open" else "yellow"
            cb_parts.append(f"[{color}]{provider}={s}[/{color}] (fails={fails})")
        _console.print(f"LLM CB: {'  '.join(cb_parts)}")
    else:
        _console.print("LLM CB: [dim](no data)[/dim]")

    # price_provider
    pp = data.get("price_provider")
    if pp:
        _console.print(f"Price provider: [cyan]{pp}[/cyan]")

    # snapshots (per tradeable instrument)
    snaps = data.get("snapshots") or []
    if snaps:
        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        tbl.add_column("ペア")
        tbl.add_column("最新ok分析")
        tbl.add_column("経過 (min)", justify="right")
        for s in snaps:
            sym = s.get("symbol", "?")
            if s.get("error"):
                tbl.add_row(sym, f"[red]error: {s['error'][:40]}[/red]", "-")
                continue
            latest = s.get("latest_at")
            age = s.get("age_minutes")
            if latest and age is not None:
                latest_short = latest[:16].replace("T", " ")
                age_color = (
                    "green" if age < 90
                    else "yellow" if age < 240
                    else "red"
                )
                tbl.add_row(sym, latest_short, f"[{age_color}]{age:.0f}[/{age_color}]")
            else:
                tbl.add_row(sym, "[dim](no recent ok)[/dim]", "-")
        _console.print(tbl)

    # MT5 bridge
    mt5b = data.get("mt5_bridge") or {}
    if not mt5b.get("configured"):
        _console.print("[dim]MT5 bridge: 未設定[/dim]\n")
        return
    # reconciliation skip 連続回数 (bridge 到達可否に関わらず /status に含まれる。
    # 不通時こそ確認したい指標なので unreachable 経路でも表示する)
    recon = mt5b.get("reconciliation_skipped_consecutive")
    if not mt5b.get("reachable"):
        recon_str = f"  [yellow]reconcile skip×{recon}[/yellow]" if recon else ""
        _console.print(
            f"[red]MT5 bridge unreachable: {mt5b.get('error', 'unknown')}[/red]"
            f"{recon_str}\n"
        )
        return
    mt5_conn = mt5b.get("mt5_connected")
    soft = mt5b.get("soft_halted")
    hard = mt5b.get("is_hard_halted")
    accept = mt5b.get("accepts_new_orders")
    dry = mt5b.get("dry_run")
    flags = [
        f"[{'green' if mt5_conn else 'red'}]mt5={'connected' if mt5_conn else 'disconnected'}[/]",
        f"[{'red' if soft else 'green'}]soft_halt={'yes' if soft else 'no'}[/]",
        f"[{'red' if hard else 'green'}]hard_halt={'yes' if hard else 'no'}[/]",
        f"[{'green' if accept else 'red'}]accepts={'yes' if accept else 'no'}[/]",
    ]
    if dry:
        flags.append("[yellow]DRY_RUN[/yellow]")
    if recon:
        flags.append(f"[yellow]reconcile skip×{recon}[/yellow]")
    _console.print(f"MT5 bridge: {' | '.join(flags)}\n")


def _cmd_account() -> None:
    """残高 + 損益 + ポジション + MT5 実残高 (halt は ?status で確認)。"""
    data = _get("/account")
    if data is None:
        return

    internal = data.get("internal") or {}
    pnl = internal.get("pnl", 0.0)
    pnl_pct = internal.get("pnl_pct", 0.0)
    drawdown = internal.get("drawdown_pct", 0.0)
    pnl_color = "green" if pnl >= 0 else "red"
    dd_color = "yellow" if drawdown > 5 else "dim"
    _console.print(
        f"\n残高: [bold]{internal.get('balance', 0):,.2f}[/bold]  "
        f"[{pnl_color}]({pnl:+.2f} / {pnl_pct:+.2f}%)[/{pnl_color}]"
        f"  [{dd_color}]DD={drawdown:.2f}%[/{dd_color}]"
        f"  取引: {internal.get('total_trades', 0)}回  "
        f"勝率: {internal.get('win_rate', 0)}%"
    )

    # MT5 実残高 / 乖離 (live mode のみ)
    mt5 = data.get("mt5")
    if mt5 is not None:
        bal = mt5.get("balance")
        eq = mt5.get("equity")
        fm = mt5.get("free_margin")
        margin = mt5.get("margin")
        parts = []
        if bal is not None:
            parts.append(f"balance=[cyan]{bal:,.2f}[/cyan]")
        if eq is not None:
            parts.append(f"equity=[cyan]{eq:,.2f}[/cyan]")
        if fm is not None:
            parts.append(f"free_margin=[cyan]{fm:,.2f}[/cyan]")
        if margin is not None:
            parts.append(f"margin=[cyan]{margin:,.2f}[/cyan]")
        if parts:
            _console.print(f"[dim]MT5:[/dim] {'  '.join(parts)}")
    div = data.get("divergence")
    if div is not None:
        diff = div.get("balance_diff", 0.0)
        diff_pct = div.get("balance_diff_pct")
        color = "yellow" if abs(diff) > 100 else "dim"
        pct_str = f"{diff_pct:+.2f}%" if diff_pct is not None else "N/A"
        _console.print(f"[{color}]乖離: {diff:+.2f} ({pct_str})[/{color}]")

    # ポジション
    positions = internal.get("open_positions") or []
    if not positions:
        _console.print("[dim]オープンポジションなし[/dim]\n")
        return

    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("ペア")
    tbl.add_column("方向", justify="center")
    tbl.add_column("エントリー", justify="right")
    tbl.add_column("SL", justify="right")
    tbl.add_column("TP", justify="right")
    tbl.add_column("現在値", justify="right")
    tbl.add_column("損益", justify="right")
    for pos in positions:
        upnl = pos.get("unrealized_pnl")
        upnl_str = (
            f"[{'green' if upnl >= 0 else 'red'}]{upnl:+.2f}[/]"
            if upnl is not None else "N/A"
        )
        tbl.add_row(
            pos["pair"],
            "📈 BUY" if pos["direction"] == "buy" else "📉 SELL",
            f"{pos['entry_price']:.5f}",
            f"{pos['stop_loss']:.5f}",
            f"{pos['take_profit']:.5f}",
            f"{pos['current_price']:.5f}" if pos.get("current_price") else "N/A",
            upnl_str,
        )
    _console.print(tbl)


def _cmd_news() -> None:
    data = _get("/news")
    if data is None:
        return
    cats = data.get("categories", {})
    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("カテゴリ")
    tbl.add_column("スコア", justify="right")
    tbl.add_column("信頼度", justify="right")
    tbl.add_column("サマリー")
    for cat, info in cats.items():
        if info is None:
            tbl.add_row(cat, "—", "—", "[dim]データなし[/dim]")
        else:
            score = info.get("sentiment_score", 0) or 0
            score_color = "green" if score > 0 else ("red" if score < 0 else "white")
            tbl.add_row(
                cat,
                f"[{score_color}]{score:+.2f}[/{score_color}]",
                f"{info.get('confidence', 0):.2f}",
                (info.get("summary", "") or "")[:80],
            )
    _console.print(tbl)


def _cmd_tech() -> None:
    """銘柄別の最新テクニカルスナップショット。

    /tech は銘柄ごとに ``latest_collect`` (全 status の最新収集行) と
    ``latest_ok`` (最新の成功分析行) を入れ子で返す。表は ``latest_ok`` を
    表示し、``latest_ok`` が無い銘柄は ``latest_collect`` の status を出す。
    """
    data = _get("/tech")
    if data is None:
        return
    snaps = data.get("snapshots", [])
    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("銘柄")
    tbl.add_column("方向", justify="center")
    tbl.add_column("スコア", justify="right")
    tbl.add_column("信頼度", justify="right")
    tbl.add_column("分析日時")
    for s in snaps:
        name = s.get("display_name") or s.get("symbol") or "—"
        ok = s.get("latest_ok")
        if not ok:
            # latest_ok が無い銘柄: 最新収集行の status をヒント表示
            collect = s.get("latest_collect") or {}
            status = collect.get("collect_status") or "no data"
            tbl.add_row(name, f"[dim]{status}[/dim]", "—", "—", "—")
            continue
        bias = ok.get("direction_bias") or "—"
        score = ok.get("bias_score")
        score_str = f"{score:+.3f}" if score is not None else "—"
        score_color = "green" if (score or 0) > 0 else ("red" if (score or 0) < 0 else "white")
        conf = ok.get("confidence")
        conf_str = f"{conf:.2f}" if conf is not None else "—"
        analyzed = (ok.get("analyzed_at") or "—")[:16].replace("T", " ")
        tbl.add_row(
            name,
            bias.upper(),
            f"[{score_color}]{score_str}[/{score_color}]",
            conf_str,
            analyzed,
        )
    _console.print(tbl)


def _cmd_analyze() -> None:
    data = _get("/analyze")
    if data is None:
        return
    signals = data.get("signals", [])
    if not signals:
        _console.print(f"[dim]{data.get('message', 'シグナルなし')}[/dim]")
        return
    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("ペア")
    tbl.add_column("アクション", justify="center")
    tbl.add_column("スコア", justify="right")
    tbl.add_column("信頼度", justify="right")
    tbl.add_column("理由")
    for sig in signals:
        action = sig.get("action", "HOLD")
        action_color = "green" if action == "BUY" else ("red" if action == "SELL" else "dim")
        score = sig.get("combined_score", 0)
        score_color = "green" if score > 0 else ("red" if score < 0 else "white")
        tbl.add_row(
            sig.get("pair", "—"),
            f"[{action_color}]{action}[/{action_color}]",
            f"[{score_color}]{score:+.4f}[/{score_color}]",
            f"{sig.get('confidence', 0):.4f}",
            (sig.get("signal_reason") or "")[:60],
        )
    _console.print(tbl)


def _cmd_run_trade() -> None:
    """取引判定ループを実行。

    /run/trade のレスポンスは 2 形態:
      - ``status="completed"``: 同期完了。balance / total_trades /
        open_positions_count を表示。
      - ``status="accepted"``: 通知 ON かつ soft_timeout 超過。バックグラウンド
        継続中 → notice を表示して終了 (完了は Discord 通知)。
    """
    _console.print("[cyan]取引判定ループを実行中... (完了まで待機)[/cyan]")
    data = _post("/run/trade")
    if data is None:
        return
    if data.get("status") == "accepted":
        _console.print(
            f"\n[yellow]{data.get('message') or '取引サイクルを実行中です。'}[/yellow]\n"
        )
        return
    elapsed = data.get("elapsed_seconds", "—")
    balance = data.get("balance")
    balance_str = f"{balance:,.2f}" if isinstance(balance, (int, float)) else "—"
    _console.print(
        f"\n[green]完了[/green] ({elapsed}s)  "
        f"残高: [bold]{balance_str}[/bold]  "
        f"取引数: {data.get('total_trades', '—')}  "
        f"ポジション: {data.get('open_positions_count', 0)}件\n"
    )


def _cmd_ask(message: str) -> None:
    """CLI 版: 回答は必ず Client に返させ (notify=False)、Discord 通知は発生させない。

    サーバ側が blocking で待つ間、HTTP タイムアウトは最大 ask_soft_timeout_sec×5
    (= 通常 300s) を想定するため余裕を持って 600s に。
    """
    _console.print("[cyan]LLMに問い合わせ中...[/cyan]  [dim](最長 5 分まで待機)[/dim]")
    data = _post(
        "/ask",
        json={"message": message, "notify": False},
        timeout=600.0,
    )
    if data is None:
        return
    status = data.get("status", "")
    response = (data.get("response") or "").strip()
    if status == "completed" and response:
        elapsed = data.get("elapsed_seconds")
        el = f"  [dim]({elapsed:.0f}s)[/dim]" if isinstance(elapsed, (int, float)) else ""
        _console.print(f"\n[bold]LLM回答:[/bold]{el}\n{response}\n")
    elif status == "accepted":
        # notify=False を指定しているので通常ここには来ない (サーバが古い場合のみ)
        _console.print(
            f"[yellow]{data.get('notice') or '受理されましたが応答待ち'}[/yellow]"
        )
    else:
        _console.print(f"[red]ask 失敗: status={status!r}[/red]\n{data!r}")


def _cmd_close(pair: str) -> None:
    data = _post(f"/close/{pair}")
    if data is None:
        return
    pnl = data.get("realized_pnl", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    _console.print(
        f"[green]決済完了: {data['pair']} {data['direction'].upper()} "
        f"@ {data['close_price']:.5f}  "
        f"[{pnl_color}]損益: {pnl:+.2f}[/{pnl_color}]  "
        f"残高: {data.get('balance', '—'):,.2f}[/green]"
    )


def _cmd_halt(args: list[str]) -> None:
    """halt soft|hard [reason] — finance API 経由で bridge を halt。"""
    if not args:
        _console.print("[red]使い方: halt soft|hard [reason][/red]")
        return
    mode = args[0].lower()
    if mode not in ("soft", "hard"):
        _console.print(f"[red]不明: halt mode={mode!r} (soft|hard)[/red]")
        return
    reason = " ".join(args[1:]) or "manual via client"

    data = _post("/admin/halt", json={"mode": mode, "reason": reason})
    if data is None:
        return
    _console.print(f"[green]halt {mode}: {reason}[/green]")


def _cmd_resume() -> None:
    """resume — finance API 経由で soft halt 解除。"""
    data = _post("/admin/resume")
    if data is None:
        return
    _console.print("[green]resumed[/green]")


def _cmd_feeds() -> None:
    """RSSフィード疎通確認（REST API経由）。"""
    data = _get("/feeds")
    if data is None:
        return

    _status_map = {
        "ok":     "[green]OK[/green]",
        "empty":  "[yellow]空[/yellow]",
        "failed": "[red]NG[/red]",
    }

    _console.print()
    for label, cat in data.get("categories", {}).items():
        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1), title=f"[bold]{label}[/bold]")
        tbl.add_column("フィード")
        tbl.add_column("状態", justify="center")
        tbl.add_column("件数", justify="right")
        tbl.add_column("最新記事")

        for feed_info in cat.get("feeds", []):
            status_display = _status_map.get(feed_info["status"], feed_info["status"])
            tbl.add_row(
                feed_info["name"],
                status_display,
                str(feed_info["count"]),
                feed_info["latest_title"] or "—",
            )

        _console.print(tbl)
        _console.print(
            f"  [dim]フィルタ通過: {cat['filtered_count']}件"
            f"  feeds OK={cat['feeds_ok']}/{cat['total_feeds']}[/dim]\n"
        )


def _cmd_logs(lines: int = 50) -> None:
    data = _get("/logs", params={"lines": lines})
    if data is None:
        return
    for line in data.get("lines", []):
        _console.print(line)
    total = data.get("total_lines", 0)
    returned = data.get("returned", 0)
    _console.print(f"[dim]({returned}/{total} 行)[/dim]")


# ── ホスト切替 ────────────────────────────────────────────────────

def _cmd_hosts() -> None:
    """設定済みホストプロファイル一覧を表示。"""
    if not _profiles:
        _console.print(
            "[dim]hosts.yaml が未配置です。config/hosts.yaml.example を参考に配置してください。[/dim]"
        )
        _console.print(f"現在接続中: [cyan]{_conn.profile.name}[/cyan]  {_conn.profile.url}")
        return

    tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("", width=2, justify="center")
    tbl.add_column("name", style="cyan")
    tbl.add_column("url")
    tbl.add_column("auth")
    for name, p in _profiles.items():
        marker = "●" if name == _conn.profile.name else ""
        auth = "[dim]key[/dim]" if p.api_key else "[dim]none[/dim]"
        tbl.add_row(marker, name, p.url, auth)
    _console.print(tbl)
    _console.print(f"[dim]切替: [cyan]use <name>[/cyan][/dim]")


def _fmt_ago(epoch: float | None) -> str:
    """UNIX 秒を「X分前」形式に。None は '—'。"""
    if not epoch:
        return "—"
    delta = time.time() - epoch
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta/60)}m ago"
    if delta < 86400:
        return f"{int(delta/3600)}h ago"
    return f"{int(delta/86400)}d ago"


def _cmd_usage() -> None:
    """LLM プロバイダ別の CB 状態・エラー履歴・usage_limit ヒット数を表示。

    Claude 系プロバイダ (claude-cli / claude) を使っていない場合は
    usage_limit 追跡対象外のため、その旨を明示する。
    """
    data = _get("/usage")
    if data is None:
        return

    # Claude 未使用ならバナーを出して詳細は簡略化
    if not data.get("claude_in_use", False):
        _console.print(
            "[yellow]Claude 系プロバイダ (claude-cli / claude) が設定されていないため、"
            "usage_limit 追跡対象はありません。[/yellow]"
        )
        _console.print(
            "[dim]  config/settings.yaml の llm.{news,price,reflection}_analysis.provider "
            "を確認してください。[/dim]"
        )
        # 参考情報として他プロバイダの CB 状態は表示する (health の簡易版)
        providers = data.get("providers", {}) or {}
        if not providers:
            return
        _console.print("[dim]  参考: 現在稼働中プロバイダの CB 状態:[/dim]")
        for name, info in providers.items():
            st = info.get("state", "?")
            fail = info.get("total_failure", 0)
            succ = info.get("total_success", 0)
            _console.print(
                f"    [cyan]{name}[/cyan] state={st} succ={succ} fail={fail}"
            )
        return

    providers = data.get("providers", {}) or {}
    if not providers:
        _console.print("[dim]プロバイダ記録なし (まだ LLM 呼び出しが発生していない)[/dim]")
        return

    tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        padding=(0, 1),
        title="LLM Usage / Circuit Breaker",
    )
    tbl.add_column("provider")
    tbl.add_column("state", justify="center")
    tbl.add_column("cooldown", justify="right")
    tbl.add_column("success", justify="right")
    tbl.add_column("fail", justify="right")
    tbl.add_column("usage_limit", justify="right")
    tbl.add_column("last error", overflow="fold")

    for name, info in providers.items():
        state = info.get("state", "?")
        cd = info.get("cooldown_remaining_s", 0) or 0
        state_color = {
            "CLOSED":    "green",
            "HALF_OPEN": "yellow",
            "OPEN":      "red",
        }.get(state, "white")
        cd_str = f"{cd:.0f}s" if state == "OPEN" else "—"
        succ = info.get("total_success", 0)
        fail = info.get("total_failure", 0)
        ulh = info.get("usage_limit_hits", 0)
        ulh_color = "red" if ulh > 0 else "dim"
        last = info.get("last_error") or {}
        last_str = (
            f"[dim]{_fmt_ago(last.get('at'))}[/dim] {last.get('type','')}"
            f" — {(last.get('message') or '')[:50]}"
            if last else "—"
        )
        tbl.add_row(
            name,
            f"[{state_color}]{state}[/{state_color}]",
            cd_str,
            str(succ),
            f"[{'red' if fail else 'white'}]{fail}[/]",
            f"[{ulh_color}]{ulh}[/{ulh_color}]",
            last_str,
        )
    _console.print(tbl)

    # OPEN のプロバイダがあれば詳細 (直近エラー 3 件) を下に補足
    for name, info in providers.items():
        if info.get("state") != "OPEN":
            continue
        recent = (info.get("recent_errors") or [])[-3:]
        if not recent:
            continue
        _console.print(
            f"[red]● {name} OPEN[/red]"
            f" (残 {info.get('cooldown_remaining_s', 0):.0f}s) "
            f"直近エラー:"
        )
        for ev in reversed(recent):
            _console.print(
                f"  [dim]{_fmt_ago(ev.get('at'))}[/dim]"
                f" [yellow]{ev.get('type', '?')}[/yellow]"
                f"  {(ev.get('message') or '')[:80]}"
            )


def _cmd_schedule() -> None:
    """次回実行予定 + 各カテゴリのスケジュール時刻を表示。"""
    data = _get("/schedule")
    if data is None:
        return
    if not isinstance(data, dict) or not data:
        _console.print("[dim]スケジュール情報なし[/dim]")
        return

    tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        padding=(0, 1),
        title="Schedule",
    )
    tbl.add_column("category")
    tbl.add_column("next_run")
    tbl.add_column("schedule (時刻)", overflow="fold")

    for cat, info in data.items():
        info = info or {}
        nr = (info.get("next_run") or "")[:16].replace("T", " ") or "—"
        times = info.get("schedule") or []
        times_str = ", ".join(times) if times else "—"
        tbl.add_row(cat, nr, times_str)
    _console.print(tbl)


def _cmd_use(name: str) -> None:
    """接続先を切替。"""
    if name not in _profiles:
        _console.print(
            f"[red]unknown host {name!r}[/red]  "
            f"(available: {', '.join(_profiles) or '(none)'})"
        )
        return
    with _conn.lock:
        _conn.profile = _profiles[name]
        _conn.online = True
        _conn.last_poll_error = ""
    _console.print(
        f"[green]接続先切替: [cyan]{name}[/cyan] ({_conn.profile.url})[/green]"
    )
    # 疎通確認 (/status は bridge 不通でも 200 を返す)
    data = _get("/status")
    if data is not None:
        _console.print(f"[dim]  デーモン起動: {data.get('started_at', 'N/A')}[/dim]")


# ── ログポーリングスレッド ────────────────────────────────────────

def _start_log_poller(stop_event: threading.Event, interval: float = 3.0) -> None:
    """activity.log の新着行をバックグラウンドでポーリングして表示する。

    接続失敗時は [OFFLINE] を 1 回だけ表示、復帰時に [ONLINE] を表示する。
    ホスト切替 (use コマンド) 後は last_count をリセットして新しい接続に追従する。
    """
    last_count: int | None = None
    last_profile_name: str = _conn.profile.name

    def _poll() -> None:
        nonlocal last_count, last_profile_name
        while not stop_event.wait(timeout=interval):
            # ホスト切替検知: カウンターをリセットして再同期
            if _conn.profile.name != last_profile_name:
                last_count = None
                last_profile_name = _conn.profile.name

            try:
                r = httpx.get(
                    f"{_conn.profile.url}/logs",
                    headers=_conn.headers,
                    params={"lines": 100},
                    timeout=5,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                total = data.get("total_lines", 0)
                lines = data.get("lines", [])

                # 直前までオフラインだったら復帰表示
                if not _conn.online:
                    _console.print(f"[green][ONLINE] {_conn.profile.name} に再接続しました[/green]")
                    with _conn.lock:
                        _conn.online = True
                        _conn.last_poll_error = ""

                if last_count is None:
                    last_count = total
                    continue

                new_count = total - last_count
                if new_count <= 0:
                    last_count = total
                    continue
                if new_count > 0:
                    new_lines = lines[-new_count:]
                    for line in new_lines:
                        if "[API]" not in line:
                            _console.print(f"[dim]{line}[/dim]")
                    last_count = total
            except Exception as e:
                # 初回失敗のみ表示 (連続失敗では黙る)
                err = str(e)
                if _conn.online or err != _conn.last_poll_error:
                    _console.print(
                        f"[yellow][OFFLINE] {_conn.profile.name} への接続が切れました[/yellow]"
                    )
                    _console.print(_describe_error(f"{_conn.profile.url}/logs", e))
                    with _conn.lock:
                        _conn.online = False
                        _conn.last_poll_error = err

    t = threading.Thread(target=_poll, daemon=True, name="log-poller")
    t.start()


# ── ヘルプ ────────────────────────────────────────────────────────

_HELP = """\
[bold cyan]コマンド一覧[/bold cyan]
  [cyan]status[/cyan]  (s)          — 運用状態 (プロセス / halt / LLM CB / price / snapshots / MT5 bridge)
  [cyan]account[/cyan] (acc)        — 残高 / 損益 / ポジション / MT5 実残高
  [cyan]run news[/cyan]             — 最新ニュースセンチメント
  [cyan]run tech[/cyan]             — 最新テクニカルスナップショット
  [cyan]run analyze[/cyan]          — 総合分析シグナル
  [cyan]run trade[/cyan]            — 取引判定ループを今すぐ実行
  [cyan]ask[/cyan] (メッセージ)     — FX分析LLMへ質問  例: ask USDJPYの見通しは？
  [cyan]close[/cyan] (pair)         — ポジションを手動決済  例: close USDJPY=X
  [cyan]logs[/cyan] (N)             — activity.log の末尾N行（デフォルト50）
  [cyan]feeds[/cyan]                — RSSフィード疎通確認
  [cyan]usage[/cyan]                — LLM使用量 / CB状態 / usage_limit集計
  [cyan]schedule[/cyan]             — スケジュール (取引/ニュース/技術/exit_check)
  [cyan]hosts[/cyan]                — ホストプロファイル一覧
  [cyan]use[/cyan] <name>           — 接続先ホスト切替  例: use remote
  [cyan]halt[/cyan] soft|hard       — 取引停止 (soft=新規のみ停止 / hard=全決済 / 理由 任意)
  [cyan]resume[/cyan]               — soft halt 解除
  [cyan]help[/cyan]   (h)           — このヘルプを表示
  [cyan]quit[/cyan]   (q)           — クライアントを終了（デーモンは継続稼働）"""


# ── コマンドディスパッチ ─────────────────────────────────────────

def _dispatch(raw: str) -> bool:
    """コマンドを実行する。終了すべき場合は False を返す。"""
    parts = raw.strip().split()
    if not parts:
        return True
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("q", "quit", "exit"):
        return False
    elif cmd in ("h", "help", "?"):
        _console.print(_HELP)
    elif cmd in ("s", "status"):
        _cmd_status()
    elif cmd in ("acc", "account"):
        _cmd_account()
    elif cmd == "usage":
        _cmd_usage()
    elif cmd == "schedule":
        _cmd_schedule()
    elif cmd == "run":
        if not args:
            _console.print("[red]使い方: run news | tech | analyze | trade[/red]")
        else:
            sub = args[0].lower()
            if sub in ("news", "n"):
                _cmd_news()
            elif sub in ("tech", "t", "technical"):
                _cmd_tech()
            elif sub in ("analyze", "a", "analysis"):
                _cmd_analyze()
            elif sub in ("trade", "tr"):
                _cmd_run_trade()
            else:
                _console.print(f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze | trade")
    elif cmd == "ask":
        if not args:
            _console.print("[red]使い方: ask <メッセージ>[/red]")
        else:
            _cmd_ask(" ".join(args))
    elif cmd == "close":
        if not args:
            _console.print("[red]使い方: close <pair>  例: close USDJPY=X[/red]")
        else:
            _cmd_close(args[0])
    elif cmd == "logs":
        n = int(args[0]) if args and args[0].isdigit() else 50
        _cmd_logs(n)
    elif cmd == "feeds":
        _cmd_feeds()
    elif cmd == "hosts":
        _cmd_hosts()
    elif cmd == "use":
        if not args:
            _console.print("[red]使い方: use <host_name>  (hosts で一覧表示)[/red]")
        else:
            _cmd_use(args[0])
    elif cmd == "halt":
        _cmd_halt(args)
    elif cmd == "resume":
        _cmd_resume()
    else:
        _console.print(f"[red]不明なコマンド: {cmd!r}[/red]  ([cyan]help[/cyan] で一覧)")

    return True


# ── エントリーポイント ────────────────────────────────────────────

def _parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="FX Trading Bot client",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--host", help="名前付きホストプロファイル (config/hosts.yaml)",
    )
    parser.add_argument(
        "--url", help="直接 URL 指定 (例: http://192.168.1.100:8811)",
    )
    # 残りの引数は非対話モードのコマンドとして扱う
    args, rest = parser.parse_known_args()
    return args, rest


def _prompt_text() -> str:
    """プロンプト表示用: 現在のホスト名を含む。"""
    name = _conn.profile.name
    return f"{name} > " if name != "default" else "> "


def main() -> None:
    global _conn, _profiles
    args, rest = _parse_args()

    profile, profiles = _resolve_initial_profile(args.host, args.url)
    _conn = ConnectionState(profile=profile)
    _profiles = profiles

    # 引数が渡された場合は 1 コマンド実行して終了 (非対話モード)
    if rest:
        _dispatch(" ".join(rest))
        return

    # 対話モード
    _console.print(Rule(
        f"[bold cyan]Finance Bot Client[/bold cyan]  "
        f"[dim]host=[cyan]{_conn.profile.name}[/cyan] {_conn.profile.url}[/dim]",
        style="cyan",
    ))

    # 接続確認 (/status は bridge 不通でも 200 を返す)
    data = _get("/status")
    if data is None:
        sys.exit(1)
    started = data.get("started_at", "N/A")
    _console.print(f"[green]● 接続成功[/green]  デーモン起動: [cyan]{started}[/cyan]")
    if _profiles:
        other = [n for n in _profiles if n != _conn.profile.name]
        if other:
            _console.print(f"[dim]他のホスト: {', '.join(other)}  (use <name> で切替)[/dim]")
    _console.print("[dim]コマンド入力モード — [cyan]help[/cyan] で一覧  [cyan]quit[/cyan] で終了[/dim]\n")

    stop_event = threading.Event()
    _start_log_poller(stop_event)

    try:
        while True:
            try:
                raw = pt_prompt(_prompt_text()).strip()
            except (EOFError, KeyboardInterrupt):
                _console.print("\n[dim]切断します...[/dim]")
                break
            if not _dispatch(raw):
                _console.print("[dim]切断します（デーモンは継続稼働中）[/dim]")
                break
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
