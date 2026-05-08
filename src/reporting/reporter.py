from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich import box

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import AccountState, Order
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.config import InstrumentConfig
    from src.data.analysis_store import _TechnicalSnapshot

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


def _format_age(snap_at, now):
    """analyzed_at から age を 'Xm ago' / 'Xh ago' / 'Xd ago' で返す。"""
    delta = now - snap_at
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec}s ago"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h ago"
    days = hours / 24
    return f"{days:.1f}d ago"


_STATUS_GLYPH = {
    "ok":          "[green]✓ ok[/green]",
    "stale_price": "[yellow]⚠ stale_price[/yellow]",
    "failed":      "[red]✗ failed[/red]",
}


def print_tech_summary(
    rows: list[tuple["InstrumentConfig", "_TechnicalSnapshot | None", "_TechnicalSnapshot | None"]],
) -> None:
    """銘柄別の最新 collect 行 + 最新 ok 行を二段表示する。

    Args:
        rows: (instrument, latest_collect_row | None, latest_ok_row | None) のリスト
    """
    console.print()
    console.print(Rule(
        "[bold cyan]Technical Snapshots[/bold cyan]  "
        "[dim](Latest collection attempt + last successful analysis)[/dim]",
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
    tbl.add_column("Pair",     width=10)
    tbl.add_column("Mode",     width=6)
    tbl.add_column("Collect",  width=18)
    tbl.add_column("Status",   width=22)
    tbl.add_column("Last ok",  width=18)
    tbl.add_column("Bias",     width=7,  justify="right")
    tbl.add_column("Conf",     width=5,  justify="right")
    tbl.add_column("Dir",      width=10)
    tbl.add_column("Reason / Notes", min_width=30, style="dim")

    now = db_now()
    reasons_below = []  # 非 ok の reason を表の下に列挙

    for inst, latest_collect, latest_ok in rows:
        name = inst.display_name
        mode = getattr(inst, "mode", "—")

        if latest_collect is None and latest_ok is None:
            tbl.add_row(name, mode, "[dim](no data)[/dim]", "—", "—", "—", "—", "—", "")
            continue

        # Collect 列
        if latest_collect is not None:
            collect_at = latest_collect.analyzed_at.strftime("%m-%d %H:%M")
            collect_age = _format_age(latest_collect.analyzed_at, now)
            collect_str = f"{collect_at} ({collect_age})"
            status_str = _STATUS_GLYPH.get(
                latest_collect.collect_status,
                f"? {latest_collect.collect_status}",
            )
            if latest_collect.collect_status != "ok":
                reasons_below.append(
                    (name, latest_collect.collect_status, latest_collect.reasoning_summary or "")
                )
        else:
            collect_str = "[dim](no data)[/dim]"
            status_str = "—"

        # Last ok / Bias / Conf / Dir
        if latest_ok is not None:
            ok_at = latest_ok.analyzed_at.strftime("%m-%d %H:%M")
            ok_age = _format_age(latest_ok.analyzed_at, now)
            ok_str = f"{ok_at} ({ok_age})"
            bias_str = f"{latest_ok.bias_score:+.2f}" if latest_ok.bias_score is not None else "—"
            conf_str = f"{latest_ok.confidence:.2f}" if latest_ok.confidence is not None else "—"
            dir_str = latest_ok.direction_bias or "—"
        else:
            ok_str = "[dim](no recent ok)[/dim]"
            bias_str = "—"
            conf_str = "—"
            dir_str = "—"

        notes = ""
        if latest_collect is not None and latest_collect.collect_status != "ok":
            notes = (latest_collect.reasoning_summary or "")[:60]

        tbl.add_row(
            name, mode, collect_str, status_str, ok_str,
            bias_str, conf_str, dir_str, notes,
        )

    console.print(tbl)

    if reasons_below:
        console.print()
        console.print("[dim]Status legend: ✓ ok = analysis succeeded | "
                      "⚠ stale_price = price data too old | "
                      "✗ failed = error during analysis[/dim]")
        console.print("[dim]Reasons (non-ok):[/dim]")
        for name, status, reason in reasons_below:
            console.print(f"  [dim]{name} ({status}): {reason}[/dim]")
    console.print()

