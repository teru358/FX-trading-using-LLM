from __future__ import annotations

import asyncio
import sys
import threading

import schedule
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from src.config import BASE_DIR, _DEFAULT_OLLAMA_MODEL, load_config
from src.data.analysis_store import AnalysisStore
from src.data.price_fetcher import fetch_current_price
from src.data.price_store import PriceStore
from src.jobs.news_collector import run_news_collection
from src.jobs.price_monitor import run_price_monitor
from src.jobs.technical_collector import run_technical_collection
from src.logging_setup import setup_logging
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.rag.vector_store import VectorStore
from src.startup import startup_checks
from src.trading.position_manager import PositionManager
from src.trading_cycle import run_trading_cycle

_console = Console()
_stop = threading.Event()
_job_lock = threading.Lock()  # スケジューラとコマンドの同時実行を防ぐ


# ── スケジューラスレッド ────────────────────────────────────────────────────

def _scheduler_loop() -> None:
    """バックグラウンドでスケジュールジョブを定期実行する。"""
    while not _stop.wait(timeout=10):
        if _job_lock.acquire(blocking=False):
            try:
                schedule.run_pending()
            finally:
                _job_lock.release()


# ── コマンドハンドラ ────────────────────────────────────────────────────────

def _cmd_status(config) -> None:
    state_store = StateStore(config.state_dir)
    pm = PositionManager(state_store, config.trading.initial_balance)
    account = pm.get_account_state()

    pnl = account.balance - account.initial_balance
    pnl_pct = pnl / account.initial_balance * 100
    pnl_color = "green" if pnl >= 0 else "red"
    _console.print(
        f"\n残高: [bold]{account.balance:,.2f}[/bold]  "
        f"[{pnl_color}]({pnl:+.2f} / {pnl_pct:+.2f}%)[/{pnl_color}]"
        f"  取引回数: {account.total_trades}"
    )

    if not account.open_positions:
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

    for pos in account.open_positions:
        try:
            current = fetch_current_price(pos.pair)
            mult = 1 if pos.direction == "buy" else -1
            upnl = (current - pos.entry_price) * pos.position_size * mult
            upnl_color = "green" if upnl >= 0 else "red"
            upnl_str = f"[{upnl_color}]{upnl:+.2f}[/{upnl_color}]"
            current_str = f"{current:.5f}"
        except Exception:
            upnl_str = "N/A"
            current_str = "N/A"

        tbl.add_row(
            pos.pair,
            "📈 BUY" if pos.direction == "buy" else "📉 SELL",
            f"{pos.entry_price:.5f}",
            f"{pos.stop_loss:.5f}",
            f"{pos.take_profit:.5f}",
            current_str,
            upnl_str,
        )

    _console.print(tbl)


def _cmd_close(config, pair_arg: str) -> None:
    async def _do() -> None:
        state_store = StateStore(config.state_dir)
        pm = PositionManager(state_store, config.trading.initial_balance)
        account = pm.get_account_state()

        pos = next(
            (p for p in account.open_positions if p.pair.upper() == pair_arg.upper()),
            None,
        )
        if pos is None:
            pairs_str = "  ".join(p.pair for p in account.open_positions) or "なし"
            _console.print(
                f"[red]ポジションが見つかりません: {pair_arg}[/red]\n"
                f"オープン中: {pairs_str}"
            )
            return

        try:
            current = fetch_current_price(pos.pair)
        except Exception as e:
            _console.print(f"[red]価格取得失敗: {e}[/red]")
            return

        closed = pm.close_position(pos.order_id, current, "manual")
        if closed:
            _console.print(
                f"[green]決済完了: {closed.pair} {closed.direction.upper()} "
                f"@ {current:.5f}  損益: {closed.realized_pnl:+.2f}[/green]"
            )
            if config.notifier.notify_on_order_close:
                notifier = create_notifier(config.notifier.notifier)
                await notifier.notify_order_closed(OrderClosedEvent(
                    pair=closed.pair,
                    direction=closed.direction,
                    entry_price=closed.entry_price,
                    close_price=current,
                    realized_pnl=closed.realized_pnl or 0.0,
                    close_reason="manual",
                    balance=pm.get_account_state().balance,
                ))

    asyncio.run(_do())


_HELP = """\
[bold cyan]コマンド一覧[/bold cyan]
  [cyan]status[/cyan]  (s)         — 残高とオープンポジションを表示
  [cyan]run news[/cyan]            — ニュース収集を今すぐ実行
  [cyan]run tech[/cyan]            — テクニカル分析を今すぐ実行
  [cyan]run trade[/cyan]           — 取引サイクルを今すぐ実行
  [cyan]run mon[/cyan]             — 価格監視を今すぐ実行
  [cyan]close <pair>[/cyan]        — ポジションを手動決済  例: close USDJPY=X
  [cyan]edit[/cyan]   (e)          — user_notes.md を vim で編集
  [cyan]help[/cyan]   (h)          — このヘルプを表示
  [cyan]quit[/cyan]   (q)          — 終了"""


def _run_commands(config, store, price_store, analysis_store) -> None:
    _console.print("[dim]コマンド入力モード — [cyan]help[/cyan] で一覧表示[/dim]\n")
    while not _stop.is_set():
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            _console.print("\n[dim]終了します...[/dim]")
            _stop.set()
            break

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("q", "quit", "exit"):
                _console.print("[dim]終了します...[/dim]")
                _stop.set()
                break
            elif cmd in ("h", "help", "?"):
                _console.print(_HELP)
            elif cmd in ("s", "status"):
                _cmd_status(config)
            elif cmd == "run":
                if not args:
                    _console.print("[red]使い方: run news | tech | trade | mon[/red]")
                    continue
                sub = args[0].lower()
                with _job_lock:
                    if sub in ("news", "n"):
                        _console.print("[cyan]ニュース収集を実行中...[/cyan]")
                        run_news_collection(config, store)
                    elif sub in ("tech", "t", "technical"):
                        _console.print("[cyan]テクニカル分析を実行中...[/cyan]")
                        run_technical_collection(config, store, price_store, analysis_store)
                    elif sub in ("trade", "trading"):
                        _console.print("[cyan]取引サイクルを実行中...[/cyan]")
                        run_trading_cycle(config, store, price_store, analysis_store)
                    elif sub in ("mon", "monitor"):
                        _console.print("[cyan]価格監視を実行中...[/cyan]")
                        run_price_monitor(config)
                    else:
                        _console.print(
                            f"[red]不明: {sub!r}[/red]  使い方: run news | tech | trade | mon"
                        )
            elif cmd in ("e", "edit"):
                import subprocess
                notes = config.user_notes_path
                _console.print(f"[cyan]vim で {notes.name} を開きます...[/cyan]")
                subprocess.call(["vim", str(notes)])
                _console.print("[green]編集完了[/green]")
            elif cmd == "close":
                if not args:
                    _console.print("[red]使い方: close <pair>  例: close USDJPY=X[/red]")
                    continue
                _cmd_close(config, args[0])
            else:
                _console.print(
                    f"[red]不明なコマンド: {cmd!r}[/red]  ([cyan]help[/cyan] で一覧)"
                )
        except KeyboardInterrupt:
            _console.print("\n[yellow]中断しました[/yellow]")
        except Exception as e:
            _console.print(f"[red]エラー: {e}[/red]")


def main() -> None:
    config = load_config()
    setup_logging(config.logging, BASE_DIR)

    if not startup_checks(config):
        sys.exit(1)

    is_fresh_start = not config.prices_db_path.exists()

    store = VectorStore(config.rag_db_path)
    price_store = PriceStore(config.prices_db_path)
    analysis_store = AnalysisStore(config.prices_db_path)

    tz = config.schedule.timezone
    news_tz = config.news_collection.timezone
    interval = config.news_collection.interval_minutes
    run_times = config.schedule.run_times

    news_times = [
        f"{h:02d}:{m:02d}"
        for h in range(24)
        for m in range(0, 60, interval)
    ]
    technical_times = [t for t in news_times if t.endswith(":00")]

    # スケジュール情報パネル
    sched_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    sched_table.add_column("Label", style="dim")
    sched_table.add_column("Value", style="bold white")
    sched_table.add_row("Pairs",           ", ".join(p.display_name for p in config.enabled_pairs))
    sched_table.add_row(
        "News collection",
        f"every [cyan]{interval}[/cyan] min  ([cyan]{news_tz}[/cyan] aligned)",
    )
    sched_table.add_row(
        "Technical analysis",
        f"every :00  (interval=[cyan]{config.trading.ohlcv_interval}[/cyan])",
    )
    sched_table.add_row("Trading cycles",  f"[cyan]{' / '.join(run_times)}[/cyan]  ({tz})")
    monitor_status = (
        f"every [cyan]{config.price_monitor.interval_minutes}[/cyan] min  "
        f"(alert≥{config.price_monitor.alert_threshold_pct:.1%}"
        + (
            f"  emergency≥{config.price_monitor.emergency_close_pct:.1%}"
            if config.price_monitor.enable_emergency_close
            else "  emergency=off"
        )
        + ")"
    )
    sched_table.add_row(
        "Price monitor",
        monitor_status if config.price_monitor.enabled else "[dim]disabled[/dim]",
    )
    def _model(role_cfg) -> str:
        return role_cfg.model or _DEFAULT_OLLAMA_MODEL
    sched_table.add_row("LLM (news)",      _model(config.llm.news_analysis))
    sched_table.add_row("LLM (price)",     _model(config.llm.price_analysis))
    sched_table.add_row("LLM (reflect)",   _model(config.llm.reflection))
    sched_table.add_row("Embed model",     config.rag.embedding_model)
    _console.print(Panel(sched_table, title="[bold cyan]Schedule[/bold cyan]", border_style="cyan", padding=(0, 1)))

    # ジョブ登録
    for t in news_times:
        schedule.every().day.at(t, news_tz).do(run_news_collection, config, store)

    for t in technical_times:
        schedule.every().day.at(t, news_tz).do(
            run_technical_collection, config, store, price_store, analysis_store
        )

    for t in run_times:
        schedule.every().day.at(t, tz).do(
            run_trading_cycle, config, store, price_store, analysis_store
        )

    if config.price_monitor.enabled:
        schedule.every(config.price_monitor.interval_minutes).minutes.do(
            run_price_monitor, config
        )

    # 起動直後にニュース収集+テクニカル分析を1回実行
    # prices.db が存在しない場合（初回起動）は市場時間を無視して強制実行
    _console.print(Rule("[dim]Initial collection[/dim]", style="dim"))
    with _job_lock:
        run_news_collection(config, store)
        run_technical_collection(config, store, price_store, analysis_store, force=is_fresh_start)

    # スケジューラをバックグラウンドスレッドで起動
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    scheduler_thread.start()

    _console.print(Rule("[dim cyan]Scheduler running[/dim cyan]", style="dim cyan"))

    # メインスレッド: コマンドループ
    _run_commands(config, store, price_store, analysis_store)


if __name__ == "__main__":
    main()
