from __future__ import annotations

import argparse
import logging
import sys
import threading

import schedule
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from src.cli import run_commands
from src.concurrency.job_guard import JobGuard
from src.concurrency.priority_job_slot import PriorityJobSlot
from src.config import BASE_DIR, load_config
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
from src.trading.market_state import market_skip_check
from src.trading_cycle import run_exit_check_cycle, run_forecast_cycle, run_trading_cycle
from src.analysis.prompt_stats import estimate_prompt_size

_console = Console()
_stop = threading.Event()
_logger = logging.getLogger("finance.main")
# LLMジョブ用の単一スロット (news/tech/trade/econ/ask/run_trade の排他制御)
_llm_slot = PriorityJobSlot("llm")

# 個別ジョブのガード (LLM 不使用の軽量ジョブ)
# FX 市場休場中はスケジューラから呼ばれても spawn 自体しない (無音スキップ)。
# econ_calendar は市場営業とは独立した日次フェッチなので予定どおり動かす。
_guards: dict[str, JobGuard] = {
    "price_monitor": JobGuard("price_monitor", skip_predicate=market_skip_check),
    "exit_check": JobGuard("exit_check", skip_predicate=market_skip_check),
    "forecast": JobGuard("forecast", skip_predicate=market_skip_check),
    "econ": JobGuard("econ_calendar"),
    "weekly_diagnosis": JobGuard("weekly_diagnosis"),
    "data_backup": JobGuard("data_backup"),
    "mt5_heartbeat": JobGuard("mt5_heartbeat"),
}


# ── スケジューラスレッド ────────────────────────────────────────────────────

def _run_with_guard(guard: JobGuard, fn, *args, **kwargs) -> None:
    """スケジューラから呼ばれたジョブをガード経由で非同期起動する。"""
    guard.spawn_if_idle(fn, *args, **kwargs)


def _run_with_slot(fn, *args, **kwargs) -> None:
    """スケジューラから呼ばれたLLMジョブをスロット経由で実行する。

    スレッドで spawn してからスロット取得を試みる (非 blocking)。
    ``_market_aware=True`` を渡すと、FX 市場休場中は無音スキップする
    (テクニカル/取引サイクルに使用)。ニュース収集などは休日でも走る想定。
    """
    market_aware = kwargs.pop("_market_aware", False)

    def _try() -> None:
        if market_aware and market_skip_check():
            return
        _llm_slot.try_run_scheduled(fn, *args, **kwargs)
    threading.Thread(
        target=_try,
        name=f"sched-{getattr(fn, '__name__', 'anon')}",
        daemon=True,
    ).start()


def _scheduler_loop() -> None:
    """バックグラウンドでスケジュールジョブを定期実行する。

    各ジョブは guard/slot 経由で自身の重複判定をするため、
    スケジューラ自身の排他制御は不要。
    """
    while not _stop.wait(timeout=10):
        schedule.run_pending()


def main() -> None:
    parser = argparse.ArgumentParser(description="FX Paper Trader")
    parser.add_argument("--skip-news", action="store_true", help="起動時の初回ニュース取得をスキップ")
    parser.add_argument("--skip-tech", action="store_true", help="起動時の初回テクニカル収集をスキップ")
    parser.add_argument("--daemon", action="store_true", help="デーモンモード: stdin CLIを起動せずREST APIのみで操作")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.logging, BASE_DIR)

    # 構成サマリ (LLM 詳細は Startup Checks パネル側で表示)
    _trade_n = len(config.tradeable_instruments)
    _watch_n = len(config.watch_only_instruments)
    _tv = "on" if config.tradingview.enabled else "off"
    _api = "on" if config.api.enabled else "off"
    _pp = config.price_provider.realtime_provider
    _logger.info(
        "[SYSTEM] Startup — mode=%s daemon=%s instruments=%dtrade/%dwatch "
        "TV=%s API=%s provider=%s",
        config.trading.trading_mode, args.daemon,
        _trade_n, _watch_n,
        _tv, _api, _pp,
    )

    if not startup_checks(config):
        _logger.error("[SYSTEM] Startup aborted — startup_checks failed")
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

    news_offset = config.news_collection.offset_minutes
    news_times = [
        f"{h:02d}:{m:02d}"
        for h in range(24)
        for m in range(news_offset, 60, interval)
    ]
    # テクニカル分析は毎時:00（ニュース時刻と独立）
    technical_times = [f"{h:02d}:00" for h in range(24)]

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
    _skipped_forecast = [t for t in forecast_times if t in run_times]
    _skip_note = f"  skip=[dim]{','.join(_skipped_forecast)}[/dim](=trade)" if _skipped_forecast else ""
    sched_table.add_row(
        "Forecast cycle",
        f"every [cyan]{forecast_interval}[/cyan]h offset=[cyan]{forecast_start:02d}:00[/cyan]  "
        f"({' / '.join(forecast_times)}){_skip_note}  (signal verify + new forecast, no LLM)",
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

    sched_table.add_row("Price provider", price_provider.status_line())
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
            schedule.every().day.at(t, tz).do(
                _run_with_guard, _guards["price_monitor"], run_price_monitor, config, price_provider
            )

    # 2. SL/TP確認・ポジション再評価（毎時:00・LLMなし）
    for t in technical_times:
        schedule.every().day.at(t, news_tz).do(
            _run_with_guard, _guards["exit_check"],
            run_exit_check_cycle, config, store, analysis_store, price_provider=price_provider
        )

    # 3. ニュース収集（LLMあり・時間がかかる）
    for t in news_times:
        schedule.every().day.at(t, news_tz).do(
            _run_with_slot, run_news_collection, config, store
        )

    # RAGクリーンアップ（毎日1回、ニュース収集の最初の時刻に実行）
    schedule.every().day.at(news_times[0], news_tz).do(_run_rag_cleanup)

    # 4. テクニカル分析（LLMあり・最も時間がかかる）
    for t in technical_times:
        schedule.every().day.at(t, news_tz).do(
            _run_with_slot,
            run_technical_collection, config, store, price_store, analysis_store,
            price_provider=price_provider,
            _market_aware=True,
        )

    # 5. 予測サイクル（LLMなし・取引判定の直前）
    #    取引判定と同時刻の場合はスキップ（取引判定が最新データで判断するため）
    run_times_set = set(run_times)
    forecast_times_filtered = [t for t in forecast_times if t not in run_times_set]
    for t in forecast_times_filtered:
        schedule.every().day.at(t, news_tz).do(
            _run_with_guard, _guards["forecast"],
            run_forecast_cycle, config, store, analysis_store, forecast_store,
            price_provider=price_provider, price_store=price_store,
        )

    # 6. 取引判定（LLMあり・指定時刻のみ）
    for t in run_times:
        schedule.every().day.at(t, tz).do(
            _run_with_slot,
            run_trading_cycle, config, store, price_store, analysis_store, hold_store,
            price_provider=price_provider,
            _market_aware=True,
        )

    # 経済指標カレンダー日次フェッチ (オプション)
    if config.economic_calendar.enabled:
        from src.data.econ_event_store import EconEventStore
        from src.jobs.econ_calendar_fetcher import run_daily_fetch as _run_daily_econ

        _econ_store = EconEventStore(config.econ_db_path)

        def _econ_daily():
            _run_daily_econ(
                _econ_store,
                lookahead_hours=config.economic_calendar.lookahead_hours,
                currencies=config.economic_calendar.currencies,
                min_importance=config.economic_calendar.min_importance,
            )

        schedule.every().day.at(
            config.economic_calendar.fetch_time,
            config.economic_calendar.fetch_timezone,
        ).do(_run_with_guard, _guards["econ"], _econ_daily)

    # 週次自己診断レポート (FX 休場の週末に実行、cron 不使用)
    if config.weekly_diagnosis.enabled:
        from src.jobs.weekly_diagnosis import run_weekly_diagnosis as _run_weekly

        _wd_cfg = config.weekly_diagnosis
        _wd_weekday = _wd_cfg.weekday.lower().strip()
        _wd_picker = getattr(schedule.every(), _wd_weekday, None)
        if _wd_picker is None:
            _logger.warning(
                f"[WEEKLY] invalid weekday {_wd_cfg.weekday!r}, skipping schedule registration"
            )
        else:
            def _weekly_diagnosis_run():
                _run_weekly(config, forecast_store)

            _wd_picker.at(_wd_cfg.at_time, news_tz).do(
                _run_with_guard, _guards["weekly_diagnosis"], _weekly_diagnosis_run,
            )
            _logger.info(
                f"[WEEKLY] Scheduled: {_wd_weekday} {_wd_cfg.at_time} ({news_tz})"
            )

    # data/ 定期バックアップ (sync 失敗・破損対策、cron 不使用)
    if config.data_backup.enabled:
        from src.jobs.data_backup import run_data_backup as _run_backup

        _bk_cfg = config.data_backup

        def _data_backup_run():
            _run_backup(config)

        schedule.every().day.at(_bk_cfg.at_time, news_tz).do(
            _run_with_guard, _guards["data_backup"], _data_backup_run,
        )
        _logger.info(
            f"[BACKUP] Scheduled: daily {_bk_cfg.at_time} ({news_tz}), "
            f"keep {_bk_cfg.retention_count} archives in {_bk_cfg.output_dir}"
        )

    # MT5 ブリッジ稼働率測定 heartbeat (Phase 1 — 発注機能はまだ含めない)
    if config.mt5_bridge.enabled:
        from src.jobs.mt5_heartbeat import run_mt5_heartbeat as _run_mt5_hb

        _mt5_cfg = config.mt5_bridge

        def _mt5_heartbeat_run():
            _run_mt5_hb(config)

        schedule.every(_mt5_cfg.heartbeat_interval_minutes).minutes.do(
            _run_with_guard, _guards["mt5_heartbeat"], _mt5_heartbeat_run,
        )
        _logger.info(
            f"[MT5_HB] Scheduled: every {_mt5_cfg.heartbeat_interval_minutes}min "
            f"→ {_mt5_cfg.bridge_url or '(unset)'}/health"
        )

    # REST API サーバー（有効時のみ — Initial collection 前に起動）
    if config.api.enabled:
        from src.api.server import start_api_server
        start_api_server(config, store, analysis_store, _llm_slot,
                         price_store, hold_store, forecast_store)

    # 起動直後にニュース収集+テクニカル分析を1回実行
    # prices.db が存在しない場合（初回起動）は市場時間を無視して強制実行
    _console.print(Rule("[dim]Initial collection[/dim]", style="dim"))

    # 休場中起動の明示表示 (同時に MarketStateTracker の初期状態ログも発行される)
    if market_skip_check():
        _console.print(
            "[yellow]Market is currently closed.[/yellow] "
            "[dim]Price-dependent jobs (price_monitor / exit_check / forecast / "
            "technical / trading) will stay paused until market open.[/dim]"
        )

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

    _logger.info("[SYSTEM] Ready — scheduler running, entering main loop")

    # メインスレッド: コマンドループ or デーモン待機
    try:
        if args.daemon:
            _console.print("[dim]daemonモード稼働中 — REST API で操作してください (Ctrl+C で終了)[/dim]")
            try:
                _stop.wait()
            except KeyboardInterrupt:
                _console.print("\n[dim]終了します...[/dim]")
                _stop.set()
        else:
            run_commands(config, store, analysis_store, _stop, _llm_slot, forecast_store, price_store, hold_store, price_provider=price_provider)
    finally:
        _stop.set()
        # 共有 CDP クライアントを閉じる (描画経路で接続している場合)
        if config.tradingview.enabled:
            try:
                import asyncio as _asyncio
                from src.tradingview.cdp_client import shutdown_shared_cdp_clients
                _asyncio.run(shutdown_shared_cdp_clients())
            except Exception as e:
                _logger.debug(f"[SYSTEM] CDP shutdown error: {e}")
        _logger.info("[SYSTEM] Shutdown — FX Paper Trader stopped")


if __name__ == "__main__":
    main()
