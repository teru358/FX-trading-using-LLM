from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich import box

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import AccountState, Order

_CATEGORY_LABELS = {"fx": "FX Market", "global": "Global Economy", "japan": "Japan / JPY"}

console = Console()


# ── ヘルパー ──────────────────────────────────────────────

def _pnl_text(value: float) -> Text:
    t = Text(f"{value:+.2f}")
    t.stylize("bold green" if value >= 0 else "bold red")
    return t


def _action_text(action: str) -> Text:
    styles = {"buy": "bold green", "sell": "bold red", "hold": "dim yellow"}
    t = Text(action.upper())
    t.stylize(styles.get(action, "white"))
    return t


def _direction_text(action: str, predicted_direction: str) -> Text:
    """BUY/SELLは action を、HOLDは predicted_direction を表示する。"""
    if action == "buy":
        return Text("▲ bullish", style="green")
    if action == "sell":
        return Text("▼ bearish", style="red")
    # hold
    if predicted_direction == "bullish":
        return Text("▲ bullish", style="dim green")
    if predicted_direction == "bearish":
        return Text("▼ bearish", style="dim red")
    return Text("─ neutral", style="dim white")


def _score_text(score: float) -> Text:
    t = Text(f"{score:+.3f}")
    t.stylize("green" if score > 0.1 else ("red" if score < -0.1 else "yellow"))
    return t


def _ichi_text(signal: str) -> Text:
    styles = {
        "strong_bullish": "bold green",
        "bullish": "green",
        "neutral": "dim white",
        "bearish": "red",
        "strong_bearish": "bold red",
    }
    t = Text(signal.replace("_", " "))
    t.stylize(styles.get(signal, "white"))
    return t


# ── サイクルサマリー ─────────────────────────────────────

def print_run_summary(
    signals: list[TradeSignal],
    executed_orders: list[Order],
    closed_this_run: list[Order],
    account_state: AccountState,
    run_start: datetime,
) -> None:
    duration = (datetime.now() - run_start).total_seconds()
    pnl_change = account_state.balance - account_state.initial_balance
    pnl_pct = pnl_change / account_state.initial_balance * 100

    console.print()
    console.print(Rule(
        f"[bold cyan]FX Paper Trader[/bold cyan]  "
        f"[dim]{run_start.strftime('%Y-%m-%d %H:%M')}[/dim]  "
        f"[dim]{duration:.0f}s[/dim]",
        style="cyan",
    ))

    # ── シグナルテーブル ──
    if signals:
        sig_table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold bright_white on grey23",
            border_style="grey50",
            title="[bold]Signals[/bold]",
            title_style="bold cyan",
            expand=False,
            padding=(0, 1),
        )
        sig_table.add_column("Pair",      style="bold white", width=9)
        sig_table.add_column("Action",    width=6,  justify="center")
        sig_table.add_column("Direction", width=11, justify="left")
        sig_table.add_column("Score",     width=7,  justify="right")
        sig_table.add_column("Conf",      width=5,  justify="right")
        sig_table.add_column("Entry",     width=10, justify="right")
        sig_table.add_column("SL",        width=10, justify="right")
        sig_table.add_column("TP",        width=10, justify="right")
        sig_table.add_column("Reason",    style="dim", min_width=20)

        for sig in signals:
            is_active = sig.action != "hold"
            sig_table.add_row(
                sig.price.pair,
                _action_text(sig.action),
                _direction_text(sig.action, sig.predicted_direction),
                _score_text(sig.combined_score),
                f"{sig.confidence:.2f}",
                f"{sig.entry_price:.5f}" if is_active else "[dim]-[/dim]",
                f"{sig.stop_loss:.5f}"   if is_active else "[dim]-[/dim]",
                f"{sig.take_profit:.5f}" if is_active else "[dim]-[/dim]",
                f"[dim]{sig.signal_reason[:40]}[/dim]",
            )
        console.print(sig_table)

    # ── 新規注文 ──
    if executed_orders:
        order_table = Table(
            box=box.SIMPLE_HEAD,
            header_style="bold green",
            border_style="green",
            title="[bold green]Executed Orders[/bold green]",
            padding=(0, 1),
        )
        order_table.add_column("Pair",      width=10)
        order_table.add_column("Direction", width=6,  justify="center")
        order_table.add_column("Entry",     width=10, justify="right")
        order_table.add_column("SL",        width=10, justify="right")
        order_table.add_column("TP",        width=10, justify="right")
        order_table.add_column("Size",      width=8,  justify="right")

        for o in executed_orders:
            order_table.add_row(
                o.pair,
                _action_text(o.direction),
                f"{o.entry_price:.5f}",
                f"{o.stop_loss:.5f}",
                f"{o.take_profit:.5f}",
                f"{o.position_size:,.0f}",
            )
        console.print(order_table)

    # ── クローズ済み ──
    if closed_this_run:
        close_table = Table(
            box=box.SIMPLE_HEAD,
            header_style="bold yellow",
            title="[bold yellow]Closed This Cycle[/bold yellow]",
            padding=(0, 1),
        )
        close_table.add_column("Pair",      width=10)
        close_table.add_column("Direction", width=6,  justify="center")
        close_table.add_column("Reason",    width=12, justify="center")
        close_table.add_column("Entry",     width=10, justify="right")
        close_table.add_column("Close",     width=10, justify="right")
        close_table.add_column("PnL",       width=9,  justify="right")

        for t in closed_this_run:
            close_table.add_row(
                t.pair,
                _action_text(t.direction),
                f"[dim]{t.close_reason}[/dim]",
                f"{t.entry_price:.5f}",
                f"{t.close_price:.5f}" if t.close_price else "-",
                _pnl_text(t.realized_pnl or 0),
            )
        console.print(close_table)

    # ── 口座サマリー ──
    pnl_color = "green" if pnl_change >= 0 else "red"
    balance_line = (
        f"[bold white]${account_state.balance:,.2f}[/bold white]  "
        f"[{pnl_color}]{pnl_change:+.2f} ({pnl_pct:+.1f}%)[/{pnl_color}]"
    )
    stats_line = (
        f"Open [cyan]{len(account_state.open_positions)}[/cyan]  "
        f"Closed [cyan]{account_state.total_trades}[/cyan]  "
        f"Win [cyan]{account_state.win_rate()*100:.0f}%[/cyan]  "
        f"PF [cyan]{account_state.profit_factor():.2f}[/cyan]"
    )
    console.print(Panel(
        f"{balance_line}\n{stats_line}",
        title="[bold]Account[/bold]",
        border_style="cyan",
        expand=False,
        padding=(0, 2),
    ))
    console.print()


def print_news_summary(entries_by_category: dict[str, list[dict]], lookback_hours: int) -> None:
    """カテゴリ別最新ニュースセンチメントを表示する（保存済みデータのみ）。"""
    console.print()
    console.print(Rule(
        f"[bold cyan]News Sentiment[/bold cyan]  [dim]直近 {lookback_hours}h[/dim]",
        style="cyan",
    ))

    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold bright_white on grey23",
        border_style="grey50",
        padding=(0, 1),
        expand=False,
    )
    tbl.add_column("Category",  width=16)
    tbl.add_column("Collected", width=16, style="dim")
    tbl.add_column("Score",     width=7,  justify="right")
    tbl.add_column("Conf",      width=5,  justify="right")
    tbl.add_column("Items",     width=5,  justify="right")
    tbl.add_column("Themes",    width=32, style="dim")
    tbl.add_column("Summary",   min_width=30)

    any_data = False
    for cat in ("fx", "global", "japan"):
        label = _CATEGORY_LABELS.get(cat, cat)
        entries = entries_by_category.get(cat, [])
        if not entries:
            tbl.add_row(label, "[dim]データなし[/dim]", "-", "-", "-", "-", "[dim]-[/dim]")
            continue
        any_data = True
        meta = entries[0]["metadata"]
        score    = float(meta.get("sentiment_score", 0.0))
        conf     = float(meta.get("confidence", 0.0))
        themes   = (meta.get("key_themes", "") or "")[:32]
        summary  = (meta.get("summary", "") or "")[:60]
        n_count  = int(meta.get("news_count", 0))
        col_at   = (meta.get("collected_at", "") or "")[:16]
        tbl.add_row(
            label,
            col_at,
            _score_text(score),
            f"{conf:.2f}",
            str(n_count),
            themes,
            f"[dim]{summary}[/dim]",
        )

    console.print(tbl)
    if not any_data:
        console.print(
            "[dim yellow]ニュースデータがありません。"
            "スケジューラーによる自動収集をお待ちください。[/dim yellow]"
        )
    console.print()


def print_tech_summary(
    snapshots_by_symbol: dict[str, list],
    display_names: dict[str, str],
    lookback_hours: int,
) -> None:
    """銘柄別最新テクニカルスナップショットを表示する（保存済みデータのみ）。"""
    console.print()
    console.print(Rule(
        f"[bold cyan]Technical Snapshots[/bold cyan]  [dim]直近 {lookback_hours}h[/dim]",
        style="cyan",
    ))

    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold bright_white on grey23",
        border_style="grey50",
        padding=(0, 1),
        expand=False,
    )
    tbl.add_column("Pair",       width=10)
    tbl.add_column("Analyzed",   width=16, style="dim")
    tbl.add_column("Direction",  width=14)
    tbl.add_column("Score",      width=7,  justify="right")
    tbl.add_column("Conf",       width=5,  justify="right")
    tbl.add_column("Entry Zone", width=22, justify="right")
    tbl.add_column("SL",         width=10, justify="right")
    tbl.add_column("TP",         width=10, justify="right")
    tbl.add_column("RR",         width=5,  justify="right")
    tbl.add_column("Summary",    min_width=25, style="dim")

    any_data = False
    for symbol, snaps in snapshots_by_symbol.items():
        name = display_names.get(symbol, symbol)
        if not snaps:
            tbl.add_row(name, "[dim]データなし[/dim]", *(["-"] * 8))
            continue
        any_data = True
        s = snaps[0]
        analyzed = s.analyzed_at.strftime("%m-%d %H:%M") if s.analyzed_at else "-"
        rr_str   = f"{s.risk_reward_ratio:.1f}" if s.risk_reward_ratio else "-"
        entry_str = (
            f"{s.entry_zone_low:.5f}–{s.entry_zone_high:.5f}"
            if s.entry_zone_low and s.entry_zone_high else "-"
        )
        sl_str = f"{s.stop_loss:.5f}"  if s.stop_loss  else "-"
        tp_str = f"{s.take_profit:.5f}" if s.take_profit else "-"
        summary = (s.reasoning_summary or "")[:40]
        tbl.add_row(
            name,
            analyzed,
            _ichi_text(s.direction_bias or "neutral"),
            _score_text(s.bias_score or 0.0),
            f"{s.confidence:.2f}" if s.confidence else "-",
            entry_str,
            sl_str,
            tp_str,
            rr_str,
            summary,
        )

    console.print(tbl)
    if not any_data:
        console.print(
            "[dim yellow]テクニカルデータがありません。"
            "スケジューラーによる自動収集をお待ちください。[/dim yellow]"
        )
    console.print()


def print_daily_report(account_state: AccountState) -> None:
    pnl = account_state.balance - account_state.initial_balance
    pnl_pct = pnl / account_state.initial_balance * 100
    pnl_color = "green" if pnl >= 0 else "red"

    console.print()
    console.print(Rule("[bold cyan]FX Paper Trader — Account Report[/bold cyan]", style="cyan"))

    # 口座サマリーパネル
    summary_text = (
        f"Balance:       [bold white]${account_state.balance:,.2f}[/bold white]\n"
        f"Initial:       [dim]${account_state.initial_balance:,.2f}[/dim]\n"
        f"Total PnL:     [{pnl_color}]{pnl:+.2f} ({pnl_pct:+.1f}%)[/{pnl_color}]\n"
        f"\n"
        f"Trades:        [cyan]{account_state.total_trades}[/cyan]  "
        f"(Won: [green]{account_state.winning_trades}[/green]  "
        f"Lost: [red]{account_state.total_trades - account_state.winning_trades}[/red])\n"
        f"Win Rate:      [cyan]{account_state.win_rate()*100:.1f}%[/cyan]\n"
        f"Profit Factor: [cyan]{account_state.profit_factor():.2f}[/cyan]"
    )
    console.print(Panel(summary_text, title="[bold]Summary[/bold]", border_style="cyan", padding=(0, 2)))

    # オープンポジション
    if account_state.open_positions:
        pos_table = Table(
            box=box.ROUNDED,
            header_style="bold bright_white on grey23",
            border_style="grey50",
            title="[bold]Open Positions[/bold]",
            padding=(0, 1),
        )
        pos_table.add_column("Pair",      width=10)
        pos_table.add_column("Direction", width=6,  justify="center")
        pos_table.add_column("Entry",     width=10, justify="right")
        pos_table.add_column("SL",        width=10, justify="right")
        pos_table.add_column("TP",        width=10, justify="right")
        pos_table.add_column("Opened",    width=17, justify="right", style="dim")

        for pos in account_state.open_positions:
            pos_table.add_row(
                pos.pair,
                _action_text(pos.direction),
                f"{pos.entry_price:.5f}",
                f"{pos.stop_loss:.5f}",
                f"{pos.take_profit:.5f}",
                pos.opened_at.strftime("%m-%d %H:%M"),
            )
        console.print(pos_table)

    # 直近5件の取引履歴
    if account_state.closed_trades:
        hist_table = Table(
            box=box.SIMPLE_HEAD,
            header_style="bold yellow",
            title="[bold yellow]Recent Trades (last 5)[/bold yellow]",
            padding=(0, 1),
        )
        hist_table.add_column("Pair",      width=10)
        hist_table.add_column("Direction", width=6,  justify="center")
        hist_table.add_column("Reason",    width=12, justify="center")
        hist_table.add_column("PnL",       width=9,  justify="right")
        hist_table.add_column("Closed",    width=17, justify="right", style="dim")

        for t in account_state.closed_trades[-5:]:
            hist_table.add_row(
                t.pair,
                _action_text(t.direction),
                f"[dim]{t.close_reason}[/dim]",
                _pnl_text(t.realized_pnl or 0),
                t.closed_at.strftime("%m-%d %H:%M") if t.closed_at else "-",
            )
        console.print(hist_table)

    console.print()
