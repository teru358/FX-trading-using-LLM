from __future__ import annotations

import sys
import threading

import schedule
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from src.cli import run_commands
from src.config import BASE_DIR, _DEFAULT_OLLAMA_MODEL, load_config
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.jobs.news_collector import run_news_collection
from src.jobs.price_monitor import run_price_monitor
from src.jobs.technical_collector import run_technical_collection
from src.logging_setup import setup_logging
from src.rag.vector_store import VectorStore
from src.startup import startup_checks
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

    # REST API サーバー（有効時のみ — Initial collection 前に起動）
    if config.api.enabled:
        from src.api.server import start_api_server
        start_api_server(config, store, _job_lock)

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
    run_commands(config, store, price_store, analysis_store, _stop, _job_lock)


if __name__ == "__main__":
    main()
