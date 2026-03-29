"""FX Trading Bot — 対話型クライアント。

REST API 経由でデーモンプロセスを操作する。
バックグラウンドスレッドで activity.log をポーリングし、
新着ログをリアルタイムに表示する。

使い方:
    uv run python client.py            # 対話モード（デフォルト）
    uv run python client.py status     # 1コマンド実行して終了
    uv run python client.py logs 50    # ログ末尾50行を表示して終了
"""

from __future__ import annotations

import os
import sys
import threading
import time
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

def _load_api_config() -> tuple[str, str]:
    """(base_url, api_key) を返す。"""
    cfg_path = _BASE_DIR / "config" / "settings.yaml"
    port = 8811
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        port = cfg.get("api", {}).get("port", 8811)
    api_key = os.environ.get("API_SECRET_KEY", "")
    host = os.environ.get("FINANCE_HOST", f"localhost:{port}")
    return f"http://{host}", api_key


_BASE_URL, _API_KEY = _load_api_config()
_HEADERS = {"X-API-Key": _API_KEY}
_console = Console()

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
  [cyan]help[/cyan]   (h)           — このヘルプを表示
  [cyan]quit[/cyan]   (q)           — クライアントを終了（デーモンは継続稼働）"""


# ── HTTP ヘルパー ────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{_BASE_URL}{path}", headers=_HEADERS, params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        _console.print(f"[red]接続失敗: {_BASE_URL} に接続できません。デーモンが起動しているか確認してください。[/red]")
        return None
    except httpx.HTTPStatusError as e:
        _console.print(f"[red]HTTPエラー {e.response.status_code}: {e.response.text}[/red]")
        return None
    except Exception as e:
        _console.print(f"[red]エラー: {e}[/red]")
        return None


def _post(path: str, json: dict | None = None) -> dict[str, Any] | None:
    try:
        r = httpx.post(f"{_BASE_URL}{path}", headers=_HEADERS, json=json, timeout=300)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        _console.print(f"[red]接続失敗: {_BASE_URL} に接続できません。[/red]")
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
    _console.print(f"\n[green]● online[/green]  起動: [cyan]{started}[/cyan]")
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
    """RSSフィード疎通確認（ローカル直接実行）。"""
    import sys
    sys.path.insert(0, str(_BASE_DIR))
    from src.config import load_config
    from src.analysis.rss_fetcher import fetch_category_news

    config = load_config()
    categories = [
        ("FX",     config.news_sources.feeds_fx,     None),
        ("Global", config.news_sources.feeds_global, frozenset(k.lower() for k in config.keywords.global_keywords)),
        ("Japan",  config.news_sources.feeds_japan,  frozenset(k.lower() for k in config.keywords.japan_keywords)),
    ]

    _console.print()
    for label, feeds, kw in categories:
        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1), title=f"[bold]{label}[/bold]")
        tbl.add_column("フィード")
        tbl.add_column("状態", justify="center")
        tbl.add_column("件数", justify="right")
        tbl.add_column("最新記事")

        result = fetch_category_news(feeds, kw, freshness_hours=24, max_per_feed=3, max_total=len(feeds) * 3)

        # フィードごとに結果を集計
        from src.analysis.rss_fetcher import feed_short_name
        import feedparser
        from datetime import datetime, timezone

        for feed_url in feeds:
            short = feed_short_name(feed_url)
            try:
                feed = feedparser.parse(feed_url)
                count = len(feed.entries)
                status = "[green]OK[/green]" if count > 0 else "[yellow]空[/yellow]"
                latest = feed.entries[0].get("title", "—")[:60] if feed.entries else "—"
            except Exception as e:
                status = "[red]NG[/red]"
                count = 0
                latest = str(e)[:60]
            tbl.add_row(short, status, str(count), latest)

        _console.print(tbl)
        _console.print(f"  [dim]フィルタ通過: {result.news_count}件  feeds OK={result.feeds_ok}/{result.total_feeds}[/dim]\n")


def _cmd_logs(lines: int = 50) -> None:
    data = _get("/logs", params={"lines": lines})
    if data is None:
        return
    for line in data.get("lines", []):
        _console.print(line)
    total = data.get("total_lines", 0)
    returned = data.get("returned", 0)
    _console.print(f"[dim]({returned}/{total} 行)[/dim]")


# ── ログポーリングスレッド ────────────────────────────────────────

def _start_log_poller(stop_event: threading.Event, interval: float = 3.0) -> None:
    """activity.log の新着行をバックグラウンドでポーリングして表示する。"""
    last_count: int | None = None

    def _poll() -> None:
        nonlocal last_count
        while not stop_event.wait(timeout=interval):
            try:
                r = httpx.get(
                    f"{_BASE_URL}/logs",
                    headers=_HEADERS,
                    params={"lines": 100},
                    timeout=5,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                total = data.get("total_lines", 0)
                lines = data.get("lines", [])

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
            except Exception:
                pass

    t = threading.Thread(target=_poll, daemon=True, name="log-poller")
    t.start()


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
    else:
        _console.print(f"[red]不明なコマンド: {cmd!r}[/red]  ([cyan]help[/cyan] で一覧)")

    return True


# ── エントリーポイント ────────────────────────────────────────────

def main() -> None:
    # 引数が渡された場合は1コマンド実行して終了（非対話モード）
    if len(sys.argv) > 1:
        _dispatch(" ".join(sys.argv[1:]))
        return

    # 対話モード
    _console.print(Rule(f"[bold cyan]Finance Bot Client[/bold cyan]  [dim]{_BASE_URL}[/dim]", style="cyan"))

    # 接続確認
    data = _get("/health")
    if data is None:
        sys.exit(1)
    started = data.get("started_at", "N/A")
    _console.print(f"[green]● 接続成功[/green]  デーモン起動: [cyan]{started}[/cyan]")
    _console.print("[dim]コマンド入力モード — [cyan]help[/cyan] で一覧  [cyan]quit[/cyan] で終了[/dim]\n")

    stop_event = threading.Event()
    _start_log_poller(stop_event)

    try:
        while True:
            try:
                raw = pt_prompt("> ").strip()
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
