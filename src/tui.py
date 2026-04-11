"""Textual TUI アプリケーション。

2分割レイアウト: 上部にログ・コマンド結果、下部にコマンド入力。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from io import StringIO

from rich.console import Console
from rich.table import Table
from rich import box
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog

logger = logging.getLogger(__name__)


class TuiLogHandler(logging.Handler):
    """Python logging の出力を RichLog ウィジェットに転送するハンドラ。"""

    def __init__(self, rich_log: RichLog) -> None:
        super().__init__()
        self._rich_log = rich_log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._rich_log.write(msg)
        except Exception:
            self.handleError(record)


class TuiApp(App):
    """FX Trading Bot TUI。"""

    TITLE = "FX Trading Bot"
    CSS = """
    RichLog {
        height: 1fr;
        border-bottom: solid $accent;
    }
    Input {
        dock: bottom;
        height: 1;
    }
    """

    def __init__(
        self,
        config=None,
        store=None,
        analysis_store=None,
        stop_event: threading.Event | None = None,
        llm_slot=None,  # PriorityJobSlot
        forecast_store=None,
        price_store=None,
        hold_store=None,
        price_provider=None,
    ) -> None:
        super().__init__()
        self._config = config
        self._store = store
        self._analysis_store = analysis_store
        self._stop_event = stop_event
        # llm_slot は main.py から渡される PriorityJobSlot
        # None の場合は fallback (TUI 単体テスト等) として新規作成
        if llm_slot is None:
            from src.concurrency.priority_job_slot import PriorityJobSlot
            llm_slot = PriorityJobSlot("tui-fallback")
        self._llm_slot = llm_slot
        self._forecast_store = forecast_store
        self._price_store = price_store
        self._hold_store = hold_store
        self._price_provider = price_provider
        self._tui_handler: TuiLogHandler | None = None
        self._disabled_handlers: list[logging.Handler] = []

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=True, markup=True, wrap=True, id="log")
        yield Input(placeholder="コマンドを入力 (help で一覧)", id="cmd")

    def on_mount(self) -> None:
        """TUI起動時にログハンドラを設定する。"""
        rich_log = self.query_one("#log", RichLog)

        # TuiLogHandler を追加
        self._tui_handler = TuiLogHandler(rich_log)
        self._tui_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%H:%M:%S")
        )
        root = logging.getLogger()
        root.addHandler(self._tui_handler)

        # コンソールハンドラ（StreamHandler / RichHandler）を無効化
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                if h is not self._tui_handler:
                    root.removeHandler(h)
                    self._disabled_handlers.append(h)

        rich_log.write("[dim]TUI モード — [cyan]help[/cyan] でコマンド一覧[/dim]")

    def on_unmount(self) -> None:
        """TUI終了時にログハンドラを復元する。"""
        root = logging.getLogger()
        if self._tui_handler:
            root.removeHandler(self._tui_handler)
        for h in self._disabled_handlers:
            root.addHandler(h)

    # ── 出力ヘルパー ──────────────────────────────────────────────

    def _write(self, text: str) -> None:
        """RichLog にテキストを出力する。"""
        try:
            rich_log = self.query_one("#log", RichLog)
            rich_log.write(text)
        except Exception:
            pass

    def _write_rich(self, *renderables) -> None:
        """RichLog に Rich レンダラブル（Table等）を出力する。"""
        try:
            rich_log = self.query_one("#log", RichLog)
            for r in renderables:
                rich_log.write(r)
        except Exception:
            pass

    def _capture_output(self, func, *args, **kwargs) -> str:
        """関数のRich Console出力をキャプチャして文字列で返す。"""
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        import src.cli as cli_mod
        original = cli_mod._console
        cli_mod._console = console
        try:
            func(*args, **kwargs)
        finally:
            cli_mod._console = original
        return buf.getvalue()

    # ── コマンド入力ハンドラ ──────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """コマンド入力時のハンドラ。"""
        raw = event.value.strip()
        event.input.clear()
        if not raw:
            return
        self._write(f"[bold]> {raw}[/bold]")
        self._dispatch(raw)

    def _dispatch(self, raw: str) -> None:
        """コマンドをパースして実行する。"""
        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("q", "quit", "exit"):
            self._write("[dim]終了します...[/dim]")
            if self._stop_event:
                self._stop_event.set()
            self.exit()
            return

        if cmd in ("h", "help", "?"):
            self._write(
                "[bold cyan]コマンド一覧[/bold cyan]\n"
                "  [cyan]status[/cyan]  (s)          — 残高とオープンポジションを表示\n"
                "  [cyan]run news[/cyan]             — 最新ニュースセンチメントを表示\n"
                "  [cyan]run tech[/cyan]             — 最新テクニカルスナップショットを表示\n"
                "  [cyan]run analyze[/cyan]          — 総合分析シグナルを表示\n"
                "  [cyan]run forecast[/cyan] (pair)  — 予測サイクルデータを表示\n"
                "  [cyan]run trade[/cyan]            — 取引判定ループを実行\n"
                "  [cyan]compare[/cyan]  (pair)      — 複数モデルで分析を比較 (--no-tui モード)\n"
                "  [cyan]ask[/cyan] (メッセージ)     — FX分析LLMへ質問\n"
                "  [cyan]close[/cyan] (pair)         — ポジションを手動決済\n"
                "  [cyan]feeds[/cyan]                — RSSフィード疎通確認\n"
                "  [cyan]notify[/cyan]  (n)          — 通知テスト送信\n"
                "  [cyan]help[/cyan]   (h)           — このヘルプを表示\n"
                "  [cyan]quit[/cyan]   (q)           — 終了"
            )
            return

        if cmd in ("e", "edit"):
            self._write(
                "[yellow]TUIモードでは edit は使用できません。別ターミナルで実行してください:[/yellow]\n"
                "  vim config/user_notes.md"
            )
            return

        if cmd == "compare":
            self._write(
                "[yellow]compare は prompt_toolkit を使用するため TUI モードでは利用できません。[/yellow]\n"
                "  --no-tui モードで実行してください: python main.py --no-tui"
            )
            return

        # 重い処理はワーカーで実行
        self.run_worker(
            self._run_command(cmd, args),
            name=f"cmd-{cmd}",
            exclusive=True,
        )

    async def _run_command(self, cmd: str, args: list[str]) -> None:
        """ワーカーでコマンドを実行する。"""
        try:
            if cmd in ("s", "status"):
                self._cmd_status()
            elif cmd == "run":
                self._cmd_run(args)
            elif cmd == "ask":
                self._cmd_ask(args)
            elif cmd in ("n", "notify"):
                self._cmd_notify()
            elif cmd == "feeds":
                self._cmd_feeds()
            elif cmd == "close":
                self._cmd_close(args)
            else:
                self._write(f"[red]不明なコマンド: {cmd!r}[/red]  ([cyan]help[/cyan] で一覧)")
        except Exception as e:
            self._write(f"[red]エラー: {e}[/red]")

    # ── 個別コマンド ──────────────────────────────────────────────

    def _cmd_status(self) -> None:
        from src.data.price_fetcher import fetch_current_price
        from src.persistence.state_store import StateStore
        from src.trading.position_manager import PositionManager

        state_store = StateStore(self._config.state_dir)
        pm = PositionManager(state_store, self._config.trading.initial_balance, context="TUI_Status")
        account = pm.get_account_state()

        pnl = account.balance - account.initial_balance
        pnl_pct = pnl / account.initial_balance * 100
        pnl_color = "green" if pnl >= 0 else "red"
        self._write(
            f"\n残高: [bold]{account.balance:,.2f}[/bold]  "
            f"[{pnl_color}]({pnl:+.2f} / {pnl_pct:+.2f}%)[/{pnl_color}]"
            f"  取引回数: {account.total_trades}"
        )

        if not account.open_positions:
            self._write("[dim]オープンポジションなし[/dim]")
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
                "BUY" if pos.direction == "buy" else "SELL",
                f"{pos.entry_price:.5f}",
                f"{pos.stop_loss:.5f}",
                f"{pos.take_profit:.5f}",
                current_str,
                upnl_str,
            )
        self._write_rich(tbl)

    def _cmd_run(self, args: list[str]) -> None:
        from src.trading_cycle import (
            run_news_view, run_tech_view, run_analysis_summary,
            run_forecast_view, run_trading_cycle,
        )

        if not args:
            self._write("[red]使い方: run news | tech | analyze | forecast (pair) | trade[/red]")
            return

        sub = args[0].lower()
        if sub in ("news", "n"):
            self._write("[cyan]最新ニュースセンチメントを表示中...[/cyan]")
            output = self._capture_output(run_news_view, self._config, self._store)
            self._write(output)
        elif sub in ("tech", "t", "technical"):
            self._write("[cyan]最新テクニカルスナップショットを表示中...[/cyan]")
            output = self._capture_output(run_tech_view, self._config, self._analysis_store)
            self._write(output)
        elif sub in ("analyze", "a", "analysis"):
            self._write("[cyan]総合分析を表示中...[/cyan]")
            output = self._capture_output(run_analysis_summary, self._config, self._store, self._analysis_store)
            self._write(output)
        elif sub in ("forecast", "f"):
            if self._forecast_store is None:
                self._write("[red]forecast_store が利用できません[/red]")
            else:
                pair_filter = args[1] if len(args) > 1 else None
                output = self._capture_output(run_forecast_view, self._config, self._forecast_store, pair_filter)
                self._write(output)
        elif sub in ("trade", "tr"):
            if self._price_store is None or self._hold_store is None:
                self._write("[red]price_store / hold_store が利用できません[/red]")
            else:
                self._write("[cyan]取引判定ループを実行中 (LLMスロット取得待機)...[/cyan]")
                self._llm_slot.run_user_blocking(
                    run_trading_cycle,
                    self._config, self._store, self._price_store,
                    self._analysis_store, self._hold_store,
                    price_provider=self._price_provider,
                )
                self._write("[green]取引判定完了[/green]")
        else:
            self._write(f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze | forecast | trade")

    def _cmd_ask(self, args: list[str]) -> None:
        from src.trading_cycle import run_ask

        if not args:
            self._write("[red]使い方: ask <メッセージ>[/red]")
            return
        user_message = " ".join(args)
        self._write("[cyan]LLMに問い合わせ中 (LLMスロット取得待機)...[/cyan]")
        response = self._llm_slot.run_user_blocking(
            run_ask, user_message, self._config, self._store, self._analysis_store
        )
        self._write(f"\n[bold]LLM回答:[/bold]\n{response}\n")

    def _cmd_notify(self) -> None:
        from src.notifications.notifier import create_notifier

        async def _do():
            notifier = create_notifier(self._config.notifier.notifier)
            await notifier.send("🔔 【通知テスト】FX Trading Bot から送信しました。")

        asyncio.run(_do())
        self._write("[green]通知を送信しました[/green]")

    def _cmd_feeds(self) -> None:
        from src.cli import _cmd_feeds
        output = self._capture_output(_cmd_feeds, self._config)
        self._write(output)

    def _cmd_close(self, args: list[str]) -> None:
        from src.cli import _cmd_close
        if not args:
            self._write("[red]使い方: close <pair>  例: close USDJPY=X[/red]")
            return
        output = self._capture_output(_cmd_close, self._config, args[0])
        self._write(output)
