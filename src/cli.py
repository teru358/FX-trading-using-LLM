"""インタラクティブ CLI コマンドループ。"""

from __future__ import annotations

import asyncio
import subprocess
import threading

from prompt_toolkit import prompt as pt_prompt
from rich import box
from rich.console import Console
from rich.table import Table

from src.config import AppConfig
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.rag.vector_store import VectorStore
from src.trading.position_manager import PositionManager
from src.commands.compare_models import run_compare
from src.trading_cycle import run_trading_cycle
from src.views import run_analysis_summary, run_ask, run_forecast_view, run_news_view, run_tech_view

_console = Console()

_HELP = """\
[bold cyan]コマンド一覧[/bold cyan]
  [cyan]status[/cyan]  (s)          — 残高とオープンポジションを表示
  [cyan]run news[/cyan]             — 最新ニュースセンチメントを表示（保存済みデータ）
  [cyan]run tech[/cyan]             — 最新テクニカルスナップショットを表示（保存済みデータ）
  [cyan]run analyze[/cyan]          — 総合分析シグナルを表示（保存済みデータ）
  [cyan]run forecast[/cyan] (pair)  — 直近24hの予測サイクルデータを表示  例: run forecast EURUSD=X
  [cyan]run trade[/cyan]            — 取引判定ループを今すぐ実行（動作確認用）
  [cyan]compare[/cyan]  (pair)      — 複数モデルで分析を比較  例: compare USDJPY=X
  [cyan]ask[/cyan] (メッセージ)     — FX分析LLMへ質問・コメントを送信
  [cyan]close[/cyan] (pair)         — ポジションを手動決済  例: close USDJPY=X
  [cyan]tv[/cyan]                   — TradingViewチャートにシグナルを描画更新
  [cyan]feeds[/cyan]                — RSSフィード疎通確認
  [cyan]notify[/cyan]  (n)          — 通知テストメッセージを送信
  [cyan]edit[/cyan]   (e)           — user_notes.md を vim で編集
  [cyan]help[/cyan]   (h)           — このヘルプを表示
  [cyan]quit[/cyan]   (q)           — 終了"""


# ── 個別コマンド ────────────────────────────────────────────────────────────

def _cmd_status(config: AppConfig) -> None:
    state_store = StateStore(config.state_dir)
    pm = PositionManager(state_store, config.trading.initial_balance, context="CLI_Status")
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
            current = fetch_current_price(pos.pair).price
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
        pm = PositionManager(state_store, config.trading.initial_balance, context="CLI_Close")
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
            current = fetch_current_price(pos.pair).price
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
                    source="manual",
                ))

    asyncio.run(_do())


async def _update_tv_chart(config: AppConfig, analysis_store) -> None:
    """シグナル + オープンポジションを TradingView チャートに反映する。"""
    from src.tradingview.cdp_client import CDPClient
    from src.tradingview.pine_injector import PineInjector
    from src.tradingview.tv_payload import build_tv_pine
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager

    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(
        state_store,
        config.trading.initial_balance,
        context="CLI",
    )

    pine = build_tv_pine(config, analysis_store, position_mgr)
    if pine is None:
        _console.print("[yellow]シグナル/ポジションが無いため更新しません[/yellow]")
        return

    tv_cdp = CDPClient(host=config.tradingview.cdp_host, port=config.tradingview.cdp_port)
    if not await tv_cdp.connect():
        _console.print("[red]TradingViewに接続できません（Edge CDP起動済み？）[/red]")
        return
    try:
        injector = PineInjector(tv_cdp)
        result = await injector.inject_and_compile(pine)
        if result["success"]:
            account = position_mgr.get_account_state()
            _console.print(
                f"[green]チャート更新完了 "
                f"(positions={len(account.open_positions)})[/green]"
            )
        else:
            _console.print(f"[red]コンパイルエラー: {result['errors']}[/red]")
    finally:
        await tv_cdp.disconnect()


def _cmd_notify(config: AppConfig) -> None:
    async def _do() -> None:
        notifier = create_notifier(config.notifier.notifier)
        await notifier.send("🔔 【通知テスト】FX Trading Bot から送信しました。")

    asyncio.run(_do())
    _console.print("[green]通知を送信しました[/green]")


def _cmd_feeds(config: AppConfig) -> None:
    from src.analysis.rss_fetcher import fetch_category_news, feed_short_name
    import feedparser

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

        result = fetch_category_news(feeds, kw, freshness_hours=24)
        _console.print(tbl)
        _console.print(f"  [dim]フィルタ通過: {result.news_count}件  feeds OK={result.feeds_ok}/{result.total_feeds}[/dim]\n")


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
    llm_slot,  # PriorityJobSlot — avoid forward-import at module top; use string annotation instead
    forecast_store=None,
    price_store=None,
    hold_store=None,
    price_provider=None,
) -> None:
    _console.print("[dim]コマンド入力モード — [cyan]help[/cyan] で一覧表示[/dim]\n")
    while not stop_event.is_set():
        try:
            raw = pt_prompt("> ").strip()
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
                if sub in ("news", "n"):
                    _console.print("[cyan]最新ニュースセンチメントを表示中...[/cyan]")
                    run_news_view(config, store)
                elif sub in ("tech", "t", "technical"):
                    _console.print("[cyan]最新テクニカルスナップショットを表示中...[/cyan]")
                    run_tech_view(config, analysis_store)
                elif sub in ("analyze", "a", "analysis"):
                    _console.print("[cyan]総合分析を表示中...[/cyan]")
                    run_analysis_summary(config, store, analysis_store)
                elif sub in ("forecast", "f"):
                    if forecast_store is None:
                        _console.print("[red]forecast_store が利用できません[/red]")
                    else:
                        pair_filter = args[1] if len(args) > 1 else None
                        run_forecast_view(config, forecast_store, pair_filter)
                elif sub in ("trade", "tr"):
                    if price_store is None or hold_store is None:
                        _console.print("[red]price_store / hold_store が利用できません[/red]")
                    else:
                        _console.print("[cyan]取引判定ループを実行中 (LLMスロット取得待機)...[/cyan]")
                        llm_slot.run_user_blocking(
                            run_trading_cycle, config, store, price_store, analysis_store, hold_store
                        )
                else:
                    _console.print(
                        f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze | forecast | trade"
                    )
            elif cmd == "compare":
                pair_arg = args[0] if args else None
                run_compare(config, store, analysis_store, pair_arg)
            elif cmd == "ask":
                if not args:
                    _console.print("[red]使い方: ask <メッセージ>  例: ask 今のUSDJPYはどう見る？[/red]")
                    continue
                user_message = " ".join(args)
                _console.print("[cyan]LLMに問い合わせ中 (LLMスロット取得待機)...[/cyan]")
                response = llm_slot.run_user_blocking(
                    run_ask, user_message, config, store, analysis_store
                )
                _console.print(f"\n[bold]LLM回答:[/bold]\n{response}\n")
            elif cmd in ("n", "notify"):
                _cmd_notify(config)
            elif cmd in ("e", "edit"):
                _cmd_edit(config)
            elif cmd == "tv":
                if not config.tradingview.enabled:
                    _console.print("[red]tradingview.enabled が false です[/red]")
                    continue
                _console.print("[cyan]TradingViewチャートを更新中...[/cyan]")
                import asyncio as _aio
                _aio.run(_update_tv_chart(config, analysis_store))
            elif cmd == "feeds":
                _cmd_feeds(config)
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
            _console.print(f"[red]エラー: {type(e).__name__}: {e}[/red]")
