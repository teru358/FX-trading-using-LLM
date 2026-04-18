"""FX Trading Bot — 対話型クライアント。

REST API 経由でデーモンプロセス (ローカル or リモート) を操作する。
バックグラウンドスレッドで activity.log をポーリングし、
新着ログをリアルタイムに表示する。

使い方:
    uv run python client.py                       # 対話 (default ホスト)
    uv run python client.py --host stickpc        # 対話 (stickpc プロファイル)
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

def _get(path: str, params: dict | None = None) -> dict[str, Any] | None:
    try:
        r = httpx.get(
            f"{_conn.profile.url}{path}", headers=_conn.headers, params=params, timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        _console.print(
            f"[red]接続失敗: {_conn.profile.url} に接続できません。[/red]"
        )
        return None
    except httpx.HTTPStatusError as e:
        _console.print(f"[red]HTTPエラー {e.response.status_code}: {e.response.text}[/red]")
        return None
    except Exception as e:
        _console.print(f"[red]エラー: {e}[/red]")
        return None


def _post(path: str, json: dict | None = None) -> dict[str, Any] | None:
    try:
        r = httpx.post(
            f"{_conn.profile.url}{path}", headers=_conn.headers, json=json, timeout=300,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        _console.print(f"[red]接続失敗: {_conn.profile.url} に接続できません。[/red]")
        return None
    except httpx.HTTPStatusError as e:
        _console.print(f"[red]HTTPエラー {e.response.status_code}: {e.response.text}[/red]")
        return None
    except Exception as e:
        _console.print(f"[red]エラー: {e}[/red]")
        return None


# ── コマンド実装 ─────────────────────────────────────────────────

def _cmd_health() -> None:
    data = _get("/health")
    if data is None:
        return
    started = data.get("started_at", "N/A")
    jobs = data.get("scheduler", {}).get("jobs_count", "N/A")
    next_run = data.get("scheduler", {}).get("next_run", "N/A")
    _console.print(
        f"\n[green]● online[/green]  host: [cyan]{_conn.profile.name}[/cyan] "
        f"({_conn.profile.url})"
    )
    _console.print(f"  起動: [cyan]{started}[/cyan]")
    _console.print(f"  スケジューラ: {jobs} ジョブ  次回実行: [cyan]{next_run}[/cyan]\n")


def _cmd_status() -> None:
    data = _get("/status")
    if data is None:
        return
    pnl = data["pnl"]
    pnl_pct = data["pnl_pct"]
    pnl_color = "green" if pnl >= 0 else "red"
    _console.print(
        f"\n残高: [bold]{data['balance']:,.2f}[/bold]  "
        f"[{pnl_color}]({pnl:+.2f} / {pnl_pct:+.2f}%)[/{pnl_color}]"
        f"  取引回数: {data['total_trades']}  勝率: {data['win_rate']}%"
    )
    positions = data.get("open_positions", [])
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
        bias = s.get("direction_bias", "—") or "—"
        score = s.get("bias_score")
        score_str = f"{score:+.3f}" if score is not None else "—"
        score_color = "green" if (score or 0) > 0 else ("red" if (score or 0) < 0 else "white")
        conf = s.get("confidence")
        conf_str = f"{conf:.2f}" if conf is not None else "—"
        analyzed = (s.get("analyzed_at") or "—")[:16]
        tbl.add_row(
            s.get("display_name", s.get("symbol", "—")),
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


def _cmd_forecast(pair_filter: str | None = None) -> None:
    params = {"hours": 24}
    if pair_filter:
        params["pair"] = pair_filter
    data = _get("/forecast", params=params)
    if data is None:
        return
    forecasts = data.get("forecasts", {})
    for symbol, records in forecasts.items():
        _console.print(f"\n[bold]{symbol}[/bold]")
        if not records:
            _console.print("  [dim]データなし[/dim]")
            continue
        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        tbl.add_column("予測日時")
        tbl.add_column("方向", justify="center")
        tbl.add_column("スコア", justify="right")
        tbl.add_column("信頼度", justify="right")
        tbl.add_column("レビュー済", justify="center")
        for r in records[:10]:
            direction = r.get("predicted_direction", "—") or "—"
            score = r.get("combined_score", 0) or 0
            score_color = "green" if score > 0 else ("red" if score < 0 else "white")
            reviewed = "✓" if r.get("reviewed") else "—"
            tbl.add_row(
                (r.get("forecast_ts") or "—")[:16],
                direction.upper(),
                f"[{score_color}]{score:+.4f}[/{score_color}]",
                f"{r.get('confidence', 0):.4f}",
                reviewed,
            )
        _console.print(tbl)


def _cmd_run_trade() -> None:
    _console.print("[cyan]取引判定ループを実行中... (完了まで待機)[/cyan]")
    data = _post("/run/trade")
    if data is None:
        return
    elapsed = data.get("elapsed_seconds", "—")
    pnl = data.get("pnl", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    _console.print(
        f"\n[green]完了[/green] ({elapsed}s)  "
        f"残高: [bold]{data.get('balance', '—'):,.2f}[/bold]  "
        f"[{pnl_color}]PnL: {pnl:+.2f}[/{pnl_color}]"
        f"  ポジション: {len(data.get('open_positions', []))}件\n"
    )


def _cmd_ask(message: str) -> None:
    _console.print("[cyan]LLMに問い合わせ中...[/cyan]")
    data = _post("/ask", json={"message": message})
    if data is None:
        return
    _console.print(f"\n[bold]LLM回答:[/bold]\n{data.get('response', '')}\n")


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
    # 疎通確認
    data = _get("/health")
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
                        f"[yellow][OFFLINE] {_conn.profile.name} への接続が切れました: "
                        f"{err[:80]}[/yellow]"
                    )
                    with _conn.lock:
                        _conn.online = False
                        _conn.last_poll_error = err

    t = threading.Thread(target=_poll, daemon=True, name="log-poller")
    t.start()


# ── ヘルプ ────────────────────────────────────────────────────────

_HELP = """\
[bold cyan]コマンド一覧[/bold cyan]
  [cyan]status[/cyan]  (s)          — 残高とオープンポジションを表示
  [cyan]run news[/cyan]             — 最新ニュースセンチメント
  [cyan]run tech[/cyan]             — 最新テクニカルスナップショット
  [cyan]run analyze[/cyan]          — 総合分析シグナル
  [cyan]run forecast[/cyan] (pair)  — 予測サイクルデータ  例: run forecast USDJPY=X
  [cyan]run trade[/cyan]            — 取引判定ループを今すぐ実行
  [cyan]ask[/cyan] (メッセージ)     — FX分析LLMへ質問  例: ask USDJPYの見通しは？
  [cyan]close[/cyan] (pair)         — ポジションを手動決済  例: close USDJPY=X
  [cyan]logs[/cyan] (N)             — activity.log の末尾N行（デフォルト50）
  [cyan]feeds[/cyan]                — RSSフィード疎通確認
  [cyan]health[/cyan]               — プロセス死活確認
  [cyan]hosts[/cyan]                — ホストプロファイル一覧
  [cyan]use[/cyan] <name>           — 接続先ホスト切替  例: use stickpc
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
    elif cmd == "health":
        _cmd_health()
    elif cmd == "run":
        if not args:
            _console.print("[red]使い方: run news | tech | analyze | forecast [pair] | trade[/red]")
        else:
            sub = args[0].lower()
            if sub in ("news", "n"):
                _cmd_news()
            elif sub in ("tech", "t", "technical"):
                _cmd_tech()
            elif sub in ("analyze", "a", "analysis"):
                _cmd_analyze()
            elif sub in ("forecast", "f"):
                _cmd_forecast(args[1] if len(args) > 1 else None)
            elif sub in ("trade", "tr"):
                _cmd_run_trade()
            else:
                _console.print(f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze | forecast | trade")
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
        "--url", help="直接 URL 指定 (例: http://stream-svr.local:8811)",
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

    # 接続確認
    data = _get("/health")
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
