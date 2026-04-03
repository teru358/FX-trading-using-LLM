from __future__ import annotations

import argparse
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
from src.data.analysis_store import ForecastStore, HoldDecisionStore
from src.data.price_provider import PriceProvider
from src.trading_cycle import run_exit_check_cycle, run_forecast_cycle, run_trading_cycle
from src.analysis.prompt_stats import estimate_prompt_size

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
    parser = argparse.ArgumentParser(description="FX Paper Trader")
    parser.add_argument("--skip-news", action="store_true", help="起動時の初回ニュース取得をスキップ")
    parser.add_argument("--skip-tech", action="store_true", help="起動時の初回テクニカル収集をスキップ")
    parser.add_argument("--daemon", action="store_true", help="デーモンモード: stdin CLIを起動せずREST APIのみで操作")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.logging, BASE_DIR)

    if not startup_checks(config):
        sys.exit(1)

    is_fresh_start = not config.prices_db_path.exists()

    store = VectorStore(config.rag_db_path)
    price_store = PriceStore(config.prices_db_path)
    analysis_store = AnalysisStore(config.prices_db_path)
    forecast_store = ForecastStore(config.prices_db_path)
    hold_store = HoldDecisionStore(config.prices_db_path)
    price_provider = PriceProvider(config)

    if config.price_provider.realtime_provider == "twelvedata":
        if price_provider._td_fetcher:
            _ok = price_provider._td_fetcher.probe()
            if _ok:
                _console.print("[green][OK][/green]  Twelve Data API: connected")
            else:
                _console.print("[yellow][WARN][/yellow] Twelve Data API: probe failed — yfinance fallback")
        else:
            _console.print("[yellow][WARN][/yellow] Twelve Data API: key not set — yfinance only")

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
    trade_names = [f"[bold]{i.display_name}[/bold]" for i in config.tradeable_instruments]
    watch_names = [f"[dim]{i.display_name}[/dim]" for i in config.watch_only_instruments]
    sched_table.add_row("Instruments",     ", ".join(trade_names + watch_names))
    sched_table.add_row(
        "News collection",
        f"every [cyan]{interval}[/cyan] min  ([cyan]{news_tz}[/cyan] aligned)",
    )
    sched_table.add_row(
        "Technical analysis",
        f"every :00  (interval=[cyan]{config.trading.ohlcv_interval}[/cyan])",
    )
    sched_table.add_row(
        "Exit check",
        "every :00  (SL/TP + position review, no LLM)",
    )
    forecast_interval = config.analysis.forecast_review_interval_hours
    forecast_start = config.analysis.forecast_start_hour
    _fcast_offset = forecast_start
    _fcast_interval = forecast_interval
    forecast_times = [
        f"{(_fcast_offset + h) % 24:02d}:00"
        for h in range(0, 24, _fcast_interval)
    ]
    sched_table.add_row(
        "Forecast cycle",
        f"every [cyan]{forecast_interval}[/cyan]h offset=[cyan]{forecast_start:02d}:00[/cyan]  "
        f"({' / '.join(forecast_times)})  (signal verify + new forecast, no LLM)",
    )
    sched_table.add_row("Trading cycles",  f"[cyan]{' / '.join(run_times)}[/cyan]  ({tz})")
    monitor_status = (
        f"every [cyan]{config.price_monitor.interval_minutes}[/cyan] min  :00 aligned  "
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
    prompt_est = estimate_prompt_size(config)
    ind_parts = [f"classic={prompt_est.classic_chars}c"]
    if prompt_est.ichimoku_chars:
        ind_parts.append(f"+ichimoku={prompt_est.ichimoku_chars}c")
    if prompt_est.pattern_chars:
        ind_parts.append(f"+patterns={prompt_est.pattern_chars}c({prompt_est.active_patterns}pat)")
    sched_table.add_row(
        "Prompt budget (price)",
        f"indicators [cyan]{' '.join(ind_parts)}[/cyan] = [cyan]{prompt_est.indicator_total}c[/cyan]  "
        f"fixed total [cyan]{prompt_est.fixed_total}c[/cyan] (~[cyan]{prompt_est.fixed_tokens_est}tok[/cyan])  "
        f"[dim]+news/rag/reflect at runtime[/dim]",
    )
    def _run_rag_cleanup() -> None:
        """古いRAGエントリを削除する（24時間に1回）。"""
        deleted = store.cleanup_old_news(max_age_hours=48)
        if deleted > 0:
            _console.print(f"[dim][RAG] cleanup: {deleted} entries deleted[/dim]")

    def _model(role_cfg) -> str:
        return role_cfg.model or _DEFAULT_OLLAMA_MODEL
    sched_table.add_row("Price provider", price_provider.status_line())
    sched_table.add_row("LLM (news)",      _model(config.llm.news_analysis))
    sched_table.add_row("LLM (price)",     _model(config.llm.price_analysis))
    sched_table.add_row("LLM (reflect)",   _model(config.llm.reflection))
    sched_table.add_row("Embed model",     config.rag.embedding_model)
    _console.print(Panel(sched_table, title="[bold cyan]Schedule[/bold cyan]", border_style="cyan", padding=(0, 1)))

    # ジョブ登録（実行優先順: 軽量ジョブを先に登録 → 同時刻では先に実行される）

    # 1. 価格監視（5分ごと・LLMなし・最優先）
    if config.price_monitor.enabled:
        monitor_interval = config.price_monitor.interval_minutes
        monitor_times = [
            f"{h:02d}:{m:02d}"
            for h in range(24)
            for m in range(0, 60, monitor_interval)
        ]
        for t in monitor_times:
            schedule.every().day.at(t, tz).do(run_price_monitor, config, price_provider)

    # 2. SL/TP確認・ポジション再評価（毎時:00・LLMなし）
    for t in technical_times:
        schedule.every().day.at(t, news_tz).do(
            run_exit_check_cycle, config, store, analysis_store, price_provider=price_provider
        )

    # 3. 予測サイクル（LLMなし）
    for t in forecast_times:
        schedule.every().day.at(t, news_tz).do(
            run_forecast_cycle, config, store, analysis_store, forecast_store, price_provider=price_provider, price_store=price_store
        )

    # 4. ニュース収集（LLMあり・時間がかかる）
    for t in news_times:
        schedule.every().day.at(t, news_tz).do(run_news_collection, config, store)

    # RAGクリーンアップ（毎日1回、ニュース収集の最初の時刻に実行）
    schedule.every().day.at(news_times[0], news_tz).do(_run_rag_cleanup)

    # 5. テクニカル分析（LLMあり・最も時間がかかる）
    for t in technical_times:
        schedule.every().day.at(t, news_tz).do(
            run_technical_collection, config, store, price_store, analysis_store, price_provider=price_provider
        )

    # 6. 取引判定（LLMあり・指定時刻のみ）
    for t in run_times:
        schedule.every().day.at(t, tz).do(
            run_trading_cycle, config, store, price_store, analysis_store, hold_store, price_provider=price_provider
        )

    # REST API サーバー（有効時のみ — Initial collection 前に起動）
    if config.api.enabled:
        from src.api.server import start_api_server
        start_api_server(config, store, analysis_store, _job_lock,
                         price_store, hold_store, forecast_store)

    # 起動直後にニュース収集+テクニカル分析を1回実行
    # prices.db が存在しない場合（初回起動）は市場時間を無視して強制実行
    _console.print(Rule("[dim]Initial collection[/dim]", style="dim"))
    with _job_lock:
        if args.skip_news:
            _console.print("[dim]--skip-news: 初回ニュース取得をスキップ[/dim]")
        else:
            run_news_collection(config, store)
        if args.skip_tech:
            _console.print("[dim]--skip-tech: 初回テクニカル収集をスキップ[/dim]")
        else:
            run_technical_collection(config, store, price_store, analysis_store, force=is_fresh_start, price_provider=price_provider)

    # スケジューラをバックグラウンドスレッドで起動
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    scheduler_thread.start()

    _console.print(Rule("[dim cyan]Scheduler running[/dim cyan]", style="dim cyan"))

    # メインスレッド: コマンドループ or デーモン待機
    if args.daemon:
        _console.print("[dim]daemonモード稼働中 — REST API で操作してください (Ctrl+C で終了)[/dim]")
        try:
            _stop.wait()
        except KeyboardInterrupt:
            _console.print("\n[dim]終了します...[/dim]")
            _stop.set()
    else:
        run_commands(config, store, analysis_store, _stop, _job_lock, forecast_store, price_store, hold_store, price_provider=price_provider)


if __name__ == "__main__":
    main()
