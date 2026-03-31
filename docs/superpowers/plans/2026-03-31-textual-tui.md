# Textual TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** サーバー側CLI（`src/cli.py`）を Textual ベースの2分割TUI（ログ領域 + コマンド入力）に移行する。

**Architecture:** `src/tui.py` を新規作成。Textual の `RichLog` + `Input` ウィジェットで2分割画面を構成。カスタム `TuiLogHandler` でPython loggingの出力をRichLogに転送。`main.py` にフラグ追加でTUI/従来CLI/デーモンを選択。既存の `src/cli.py` はフォールバック用に維持。

**Tech Stack:** Python 3.12, textual, rich

**Spec:** `docs/superpowers/specs/2026-03-31-textual-tui-design.md`

---

### Task 1: textual 依存追加と動作確認

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add textual dependency**

`pyproject.toml` の `dependencies` リストに追加:

```toml
"textual>=0.86.0",
```

`rich>=14.3.3` の行の直後に追加する。

- [ ] **Step 2: Install**

Run: `cd /home/teru/project/finance && uv sync`
Expected: textual がインストールされる

- [ ] **Step 3: Verify import**

Run: `cd /home/teru/project/finance && .venv/bin/python -c "import textual; print(textual.__version__)"`
Expected: バージョン番号が表示される

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add textual dependency"
```

---

### Task 2: 最小限の TuiApp シェル

**Files:**
- Create: `src/tui.py`
- Test: `tests/test_tui.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tui.py`:

```python
"""tui.py の基本テスト。"""
import pytest


def test_tui_app_import():
    """TuiApp がインポートできること。"""
    from src.tui import TuiApp
    assert TuiApp is not None


def test_tui_app_has_widgets():
    """TuiApp が必要なウィジェットを定義していること。"""
    from src.tui import TuiApp
    app = TuiApp()
    # compose() が RichLog と Input を含むことを確認
    widgets = list(app.compose())
    widget_types = [type(w).__name__ for w in widgets]
    assert "RichLog" in widget_types
    assert "Input" in widget_types
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_tui.py -v`
Expected: ImportError — `src.tui` が存在しない

- [ ] **Step 3: Create minimal tui.py**

Create `src/tui.py`:

```python
"""Textual TUI アプリケーション。

2分割レイアウト: 上部にログ・コマンド結果、下部にコマンド入力。
"""

from __future__ import annotations

import logging
import threading
from io import StringIO

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Header, Footer

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

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=True, markup=True, wrap=True, id="log")
        yield Input(placeholder="コマンドを入力 (help で一覧)", id="cmd")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_tui.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/tui.py tests/test_tui.py
git commit -m "feat: add minimal TuiApp shell with RichLog and Input"
```

---

### Task 3: TuiLogHandler の統合とコンソールハンドラ制御

**Files:**
- Modify: `src/tui.py`

- [ ] **Step 1: Add logging setup to TuiApp.on_mount**

`src/tui.py` の `TuiApp` クラスに `on_mount` メソッドを追加:

```python
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
        self._disabled_handlers: list[logging.Handler] = []
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                if h is not self._tui_handler:
                    root.removeHandler(h)
                    self._disabled_handlers.append(h)

        rich_log.write("[dim]TUI モード — [cyan]help[/cyan] でコマンド一覧[/dim]")
```

- [ ] **Step 2: Add cleanup on shutdown**

`src/tui.py` の `TuiApp` クラスに `on_unmount` メソッドを追加:

```python
    def on_unmount(self) -> None:
        """TUI終了時にログハンドラを復元する。"""
        root = logging.getLogger()
        if hasattr(self, "_tui_handler"):
            root.removeHandler(self._tui_handler)
        for h in getattr(self, "_disabled_handlers", []):
            root.addHandler(h)
```

- [ ] **Step 3: Run all tests**

Run: `cd /home/teru/project/finance && .venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/tui.py
git commit -m "feat: add TuiLogHandler and console handler management"
```

---

### Task 4: コマンドディスパッチャ

**Files:**
- Modify: `src/tui.py`

- [ ] **Step 1: Add command dispatch and Input handler**

`src/tui.py` のインポートに追加:

```python
from textual.worker import Worker, WorkerState
```

`TuiApp` クラスに以下を追加:

```python
    def _write_output(self, text: str) -> None:
        """RichLog にテキストを出力する。"""
        rich_log = self.query_one("#log", RichLog)
        rich_log.write(text)

    def _write_rich(self, *renderables) -> None:
        """RichLog に Rich レンダラブル（Table等）を出力する。"""
        rich_log = self.query_one("#log", RichLog)
        for r in renderables:
            rich_log.write(r)

    def _capture_console_output(self, func, *args, **kwargs) -> str:
        """関数のRich Console出力をキャプチャして文字列で返す。"""
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        # 一時的に _console を差し替え
        import src.cli as cli_mod
        original = cli_mod._console
        cli_mod._console = console
        try:
            func(*args, **kwargs)
        finally:
            cli_mod._console = original
        return buf.getvalue()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """コマンド入力時のハンドラ。"""
        raw = event.value.strip()
        event.input.clear()
        if not raw:
            return
        self._write_output(f"[bold]> {raw}[/bold]")
        self._dispatch_command(raw)

    def _dispatch_command(self, raw: str) -> None:
        """コマンドをパースしてワーカースレッドで実行する。"""
        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("q", "quit", "exit"):
            self._write_output("[dim]終了します...[/dim]")
            if self._stop_event:
                self._stop_event.set()
            self.exit()
            return

        if cmd in ("h", "help", "?"):
            self._write_output(
                "[bold cyan]コマンド一覧[/bold cyan]\n"
                "  [cyan]status[/cyan]  (s)          — 残高とオープンポジションを表示\n"
                "  [cyan]run news[/cyan]             — 最新ニュースセンチメントを表示\n"
                "  [cyan]run tech[/cyan]             — 最新テクニカルスナップショットを表示\n"
                "  [cyan]run analyze[/cyan]          — 総合分析シグナルを表示\n"
                "  [cyan]run forecast[/cyan] (pair)  — 予測サイクルデータを表示\n"
                "  [cyan]run trade[/cyan]            — 取引判定ループを実行\n"
                "  [cyan]compare[/cyan]  (pair)      — 複数モデルで分析を比較\n"
                "  [cyan]ask[/cyan] (メッセージ)     — FX分析LLMへ質問\n"
                "  [cyan]close[/cyan] (pair)         — ポジションを手動決済\n"
                "  [cyan]feeds[/cyan]                — RSSフィード疎通確認\n"
                "  [cyan]notify[/cyan]  (n)          — 通知テスト送信\n"
                "  [cyan]help[/cyan]   (h)           — このヘルプを表示\n"
                "  [cyan]quit[/cyan]   (q)           — 終了"
            )
            return

        if cmd in ("e", "edit"):
            self._write_output(
                "[yellow]TUIモードでは edit は使用できません。別ターミナルで実行してください:[/yellow]\n"
                "  vim config/user_notes.md"
            )
            return

        # 重い処理はワーカースレッドで実行
        self.run_worker(
            self._run_command(cmd, args),
            name=f"cmd-{cmd}",
            exclusive=True,
        )

    async def _run_command(self, cmd: str, args: list[str]) -> None:
        """ワーカースレッドでコマンドを実行する。"""
        try:
            if cmd in ("s", "status"):
                self._run_status()
            elif cmd == "run":
                self._run_sub(args)
            elif cmd == "compare":
                self._run_compare(args)
            elif cmd == "ask":
                self._run_ask(args)
            elif cmd in ("n", "notify"):
                self._run_notify()
            elif cmd == "feeds":
                self._run_feeds()
            elif cmd == "close":
                self._run_close(args)
            else:
                self._write_output(f"[red]不明なコマンド: {cmd!r}[/red]  ([cyan]help[/cyan] で一覧)")
        except Exception as e:
            self._write_output(f"[red]エラー: {e}[/red]")
```

- [ ] **Step 2: Commit**

```bash
git add src/tui.py
git commit -m "feat: add command dispatcher with Input handler and worker thread execution"
```

---

### Task 5: 個別コマンド実装

**Files:**
- Modify: `src/tui.py`

- [ ] **Step 1: Add constructor to accept dependencies**

`TuiApp.__init__` を追加:

```python
    def __init__(
        self,
        config=None,
        store=None,
        analysis_store=None,
        stop_event: threading.Event | None = None,
        job_lock: threading.Lock | None = None,
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
        self._job_lock = job_lock or threading.Lock()
        self._forecast_store = forecast_store
        self._price_store = price_store
        self._hold_store = hold_store
        self._price_provider = price_provider
```

- [ ] **Step 2: Implement _run_status**

```python
    def _run_status(self) -> None:
        from src.data.price_fetcher import fetch_current_price
        from src.persistence.state_store import StateStore
        from src.trading.position_manager import PositionManager
        from rich.table import Table
        from rich import box

        state_store = StateStore(self._config.state_dir)
        pm = PositionManager(state_store, self._config.trading.initial_balance, context="TUI_Status")
        account = pm.get_account_state()

        pnl = account.balance - account.initial_balance
        pnl_pct = pnl / account.initial_balance * 100
        pnl_color = "green" if pnl >= 0 else "red"
        self._write_output(
            f"\n残高: [bold]{account.balance:,.2f}[/bold]  "
            f"[{pnl_color}]({pnl:+.2f} / {pnl_pct:+.2f}%)[/{pnl_color}]"
            f"  取引回数: {account.total_trades}"
        )

        if not account.open_positions:
            self._write_output("[dim]オープンポジションなし[/dim]")
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
```

- [ ] **Step 3: Implement _run_sub (run news/tech/analyze/forecast/trade)**

```python
    def _run_sub(self, args: list[str]) -> None:
        from src.trading_cycle import (
            run_news_view, run_tech_view, run_analysis_summary,
            run_forecast_view, run_trading_cycle,
        )

        if not args:
            self._write_output("[red]使い方: run news | tech | analyze | forecast (pair) | trade[/red]")
            return

        sub = args[0].lower()
        with self._job_lock:
            if sub in ("news", "n"):
                self._write_output("[cyan]最新ニュースセンチメントを表示中...[/cyan]")
                output = self._capture_console_output(run_news_view, self._config, self._store)
                self._write_output(output)
            elif sub in ("tech", "t", "technical"):
                self._write_output("[cyan]最新テクニカルスナップショットを表示中...[/cyan]")
                output = self._capture_console_output(run_tech_view, self._config, self._analysis_store)
                self._write_output(output)
            elif sub in ("analyze", "a", "analysis"):
                self._write_output("[cyan]総合分析を表示中...[/cyan]")
                output = self._capture_console_output(run_analysis_summary, self._config, self._store, self._analysis_store)
                self._write_output(output)
            elif sub in ("forecast", "f"):
                if self._forecast_store is None:
                    self._write_output("[red]forecast_store が利用できません[/red]")
                else:
                    pair_filter = args[1] if len(args) > 1 else None
                    output = self._capture_console_output(run_forecast_view, self._config, self._forecast_store, pair_filter)
                    self._write_output(output)
            elif sub in ("trade", "tr"):
                if self._price_store is None or self._hold_store is None:
                    self._write_output("[red]price_store / hold_store が利用できません[/red]")
                else:
                    self._write_output("[cyan]取引判定ループを実行中...[/cyan]")
                    run_trading_cycle(
                        self._config, self._store, self._price_store,
                        self._analysis_store, self._hold_store,
                        price_provider=self._price_provider,
                    )
                    self._write_output("[green]取引判定完了[/green]")
            else:
                self._write_output(f"[red]不明: {sub!r}[/red]  使い方: run news | tech | analyze | forecast | trade")
```

- [ ] **Step 4: Implement remaining commands**

```python
    def _run_compare(self, args: list[str]) -> None:
        self._write_output("[yellow]compare は TUI モードでは prompt_toolkit と競合するため、--no-tui モードで実行してください[/yellow]")

    def _run_ask(self, args: list[str]) -> None:
        from src.trading_cycle import run_ask

        if not args:
            self._write_output("[red]使い方: ask <メッセージ>[/red]")
            return
        user_message = " ".join(args)
        self._write_output("[cyan]LLMに問い合わせ中...[/cyan]")
        with self._job_lock:
            response = run_ask(user_message, self._config, self._store, self._analysis_store)
        self._write_output(f"\n[bold]LLM回答:[/bold]\n{response}\n")

    def _run_notify(self) -> None:
        import asyncio
        from src.notifications.notifier import create_notifier

        async def _do():
            notifier = create_notifier(self._config.notifier.notifier)
            await notifier.send("🔔 【通知テスト】FX Trading Bot から送信しました。")

        asyncio.run(_do())
        self._write_output("[green]通知を送信しました[/green]")

    def _run_feeds(self) -> None:
        from src.cli import _cmd_feeds
        output = self._capture_console_output(_cmd_feeds, self._config)
        self._write_output(output)

    def _run_close(self, args: list[str]) -> None:
        from src.cli import _cmd_close
        if not args:
            self._write_output("[red]使い方: close <pair>  例: close USDJPY=X[/red]")
            return
        output = self._capture_console_output(_cmd_close, self._config, args[0])
        self._write_output(output)
```

- [ ] **Step 5: Run all tests**

Run: `cd /home/teru/project/finance && .venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/tui.py
git commit -m "feat: implement all TUI commands with worker thread execution"
```

---

### Task 6: main.py の起動フロー変更

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add --no-tui flag to argparse**

`main.py` の `argparse` セクション（line 47-50）に追加:

```python
    parser.add_argument("--no-tui", action="store_true", help="TUI無効: 従来のコマンドラインモードで起動")
```

- [ ] **Step 2: Add TUI startup branch**

`main.py` の末尾、現在の分岐ロジック（line 225-233）を変更:

```python
    # メインスレッド: TUI or コマンドループ or デーモン待機
    if args.daemon:
        _console.print("[dim]daemonモード稼働中 — REST API で操作してください (Ctrl+C で終了)[/dim]")
        try:
            _stop.wait()
        except KeyboardInterrupt:
            _console.print("\n[dim]終了します...[/dim]")
            _stop.set()
    elif args.no_tui:
        run_commands(config, store, analysis_store, _stop, _job_lock, forecast_store, price_store, hold_store, price_provider=price_provider)
    else:
        try:
            from src.tui import TuiApp
            app = TuiApp(
                config=config,
                store=store,
                analysis_store=analysis_store,
                stop_event=_stop,
                job_lock=_job_lock,
                forecast_store=forecast_store,
                price_store=price_store,
                hold_store=hold_store,
                price_provider=price_provider,
            )
            app.run()
        except ImportError:
            _console.print("[yellow][WARN] textual がインストールされていません — 従来CLIで起動します[/yellow]")
            run_commands(config, store, analysis_store, _stop, _job_lock, forecast_store, price_store, hold_store, price_provider=price_provider)
        finally:
            _stop.set()
```

- [ ] **Step 3: Verify --no-tui works**

Run: `cd /home/teru/project/finance && .venv/bin/python main.py --no-tui --skip-news --skip-tech`
Expected: 従来のCLIモードで起動する（TUIなし）

- [ ] **Step 4: Verify TUI starts**

Run: `cd /home/teru/project/finance && .venv/bin/python main.py --skip-news --skip-tech`
Expected: Textual TUIが起動し、2分割画面が表示される

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: add --no-tui flag, default to Textual TUI"
```

---

### Task 7: 動作確認と微調整

**Files:**
- Possibly modify: `src/tui.py`

- [ ] **Step 1: Run full test suite**

Run: `cd /home/teru/project/finance && .venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: TUI manual test — help command**

Run: `cd /home/teru/project/finance && .venv/bin/python main.py --skip-news --skip-tech`
Type `help` in the Input field.
Expected: コマンド一覧が RichLog に表示される

- [ ] **Step 3: TUI manual test — status command**

Type `status` in the Input field.
Expected: 残高・ポジション情報が RichLog に表示される

- [ ] **Step 4: TUI manual test — quit command**

Type `quit` in the Input field.
Expected: TUI が終了し、プロセスが終了する

- [ ] **Step 5: TUI manual test — log streaming**

Run: `cd /home/teru/project/finance && .venv/bin/python main.py`（初回収集あり）
Expected: ログが RichLog にリアルタイムで流れる。コマンド入力欄は下部に固定されたまま。

- [ ] **Step 6: Fix any issues found during manual testing**

問題があれば修正。

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "fix: TUI manual test fixes"
```
