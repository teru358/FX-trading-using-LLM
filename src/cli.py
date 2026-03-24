"""インタラクティブ CLI コマンドループ。"""

from __future__ import annotations

import asyncio
import subprocess
import threading

from rich import box
from rich.console import Console
from rich.table import Table

from src.config import AppConfig
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.rag.vector_store import VectorStore
from src.trading.position_manager import PositionManager
from src.trading_cycle import run_analysis_summary, run_news_view, run_tech_view

_console = Console()

_HELP = """\
[bold cyan]コマンド一覧[/bold cyan]
  [cyan]status[/cyan]  (s)         — 残高とオープンポジションを表示
  [cyan]run news[/cyan]            — 最新ニュースセンチメントを表示（保存済みデータ）
  [cyan]run tech[/cyan]            — 最新テクニカルスナップショットを表示（保存済みデータ）
  [cyan]run analyze[/cyan]         — 総合分析シグナルを表示（保存済みデータ）
  [cyan]close <pair>[/cyan]        — ポジションを手動決済  例: close USDJPY=X
  [cyan]notify[/cyan]  (n)         — 通知テストメッセージを送信
  [cyan]edit[/cyan]   (e)          — user_notes.md を vim で編集
  [cyan]help[/cyan]   (h)          — このヘルプを表示
  [cyan]quit[/cyan]   (q)          — 終了"""


# ── 個別コマンド ────────────────────────────────────────────────────────────

def _cmd_status(config: AppConfig) -> None:
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


def _cmd_close(config: AppConfig, pair_arg: str) -> None:
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


def _cmd_notify(config: AppConfig) -> None:
    async def _do() -> None:
        notifier = create_notifier(config.notifier.notifier)
        await notifier.send("🔔 【通知テスト】FX Trading Bot から送信しました。")

    asyncio.run(_do())
    _console.print("[green]通知を送信しました[/green]")


def _cmd_edit(config: AppConfig) -> None:
    notes = config.user_notes_path
    _console.print(f"[cyan]vim で {notes.name} を開きます...[/cyan]")
    subprocess.call(["vim", str(notes)])
    _console.print("[green]編集完了[/green]")


# ── コマンドループ ──────────────────────────────────────────────────────────

def run_commands(
    config: AppConfig,
    store: VectorStore,
    analysis_store,
    stop_event: threading.Event,
    job_lock: threading.Lock,
) -> None:
    _console.print("[dim]コマンド入力モード — [cyan]help[/cyan] で一覧表示[/dim]\n")
    while not stop_event.is_set():
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            _console.print("\n[dim]終了します...[/dim]")
            stop_event.set()
            break

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("q", "quit", "exit"):
                _console.print("[dim]終了します...[/dim]")
                stop_event.set()
                break
            elif cmd in ("h", "help", "?"):
                _console.print(_HELP)
            elif cmd in ("s", "status"):
                _cmd_status(config)
            elif cmd == "run":
                if not args:
                    _console.print("[red]使い方: run news | tech | analyze | mon[/red]")
                    continue
                sub = args[0].lower()
                with job_lock:
                    if sub in ("news", "n"):
                        _console.print("[cyan]最新ニュースセンチメントを表示中...[/cyan]")
                        run_news_view(config, store)
                    elif sub in ("tech", "t", "technical"):
                        _console.print("[cyan]最新テクニカルスナップショットを表示中...[/cyan]")
                        run_tech_view(config, analysis_store)
                    elif sub in ("analyze", "a", "analysis"):
                        _console.print("[cyan]総合分析を表示中...[/cyan]")
                        run_analysis_summary(config, store, analysis_store)
                    else:
                        _console.print(
                            f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze"
                        )
            elif cmd in ("n", "notify"):
                _cmd_notify(config)
            elif cmd in ("e", "edit"):
                _cmd_edit(config)
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
