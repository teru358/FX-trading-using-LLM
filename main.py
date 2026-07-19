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
from src.jobs.technical_collector import (
    run_technical_collection,
    run_trade_technical_collection,
    run_watch_technical_collection,
)
from src.jobs.technical_schedule import (
    technical_times_for,
    effective_trade_times,
    build_technical_dispatch,
)
from src.logging_setup import setup_logging
from src.rag.vector_store import VectorStore
from src.startup import startup_checks
from src.data.analysis_store import HoldDecisionStore
from src.data.price_provider import PriceProvider
from src.trading.market_state import market_skip_check
from src.trading_cycle import run_exit_check_cycle, run_trading_cycle

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
    "technical": JobGuard("technical", skip_predicate=market_skip_check),
    "econ": JobGuard("econ_calendar"),
    "weekly_diagnosis": JobGuard("weekly_diagnosis"),
    "data_backup": JobGuard("data_backup"),
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


def _api_orchestrator_store(config):
    """API に渡す OrchestratorStore を返す (orchestrator 無効なら None)。

    orchestrator.enabled=False のときに OrchestratorStore を生成しない
    (不要な DB ファイル生成と、テストで config が MagicMock の場合の
    working directory 汚染を防ぐ — 実装後レビュー Low-Med)。
    """
    if not config.orchestrator.enabled:
        return None
    from src.data.orchestrator_store import OrchestratorStore
    return OrchestratorStore(config.prices_db_path)


def _build_cadence_driver(config, run_watch_tech, run_trade_tech):
    """cadence driver を組む (§5.3/§5.6, Phase1 Task B)。

    既存収集は銘柄一括 (per-pair でない) ため、driver は batch 粒度で回す:
    - resolver は実 trade/watch pair を持ち、boost は per-pair に効く。
    - driver の論理 pair は "__trade__" / "__watch__" の 2 つだけ。trade batch の有効
      interval は実 trade pair の最短 effective_interval で律速する (most-aggressive)。
    - 収集 callback は pair を無視して batch collection を回す。tick が boost を反映する
      よう、tick ごとに EconCadenceSource.refresh() を先に呼ぶ。
    """
    from src.data.econ_event_store import EconEventStore
    from src.jobs.cadence_driver import CadenceDriver
    from src.orchestrator.cadence_resolver import CadenceResolver
    from src.orchestrator.cadence_sources import EconCadenceSource
    from src.utils.clock import db_now

    trade_pairs = [i.symbol for i in config.tradeable_instruments]
    watch_pairs = [i.symbol for i in config.watch_only_instruments]
    trade_base = config.schedule.effective_trade_interval_seconds()
    watch_base = config.schedule.technical_watch_interval_hours * 3600
    boost_sec = config.schedule.cadence_boost_interval_minutes * 60

    resolver = CadenceResolver(
        trade_pairs=trade_pairs, watch_pairs=watch_pairs,
        trade_base_interval_sec=trade_base, watch_base_interval_sec=watch_base,
    )
    econ_store = EconEventStore(config.econ_db_path)
    econ_source = EconCadenceSource(
        config=config, econ_store=econ_store, resolver=resolver,
        boost_interval_sec=boost_sec, trade_pairs=trade_pairs,
    )

    # 論理 batch pair。trade batch interval は実 trade pair の最短で律速する。
    _LT, _LW = "__trade__", "__watch__"

    class _BatchResolver:
        def effective_interval(self, pair, now):
            pairs = trade_pairs if pair == _LT else watch_pairs
            if not pairs:
                return resolver.base_interval(pair)  # 空なら base へ (実質発火しない)
            return min(resolver.effective_interval(p, now) for p in pairs)

    def _run_watch(_pair):
        run_watch_tech("")  # batch (pair 無視)
        return True

    def _run_trade(_pair):
        run_trade_tech("")
        return True

    driver = CadenceDriver(
        resolver=_BatchResolver(),
        trade_pairs=[_LT] if trade_pairs else [],
        watch_pairs=[_LW] if watch_pairs else [],
        run_trade=_run_trade, run_watch=_run_watch,
    )

    # tick 前に econ boost を更新するラッパ driver を返す。
    class _CadenceDriver:
        def tick(self, now=None):
            now = now or db_now()
            try:
                econ_source.refresh(now)
            except Exception:
                _logger.exception("[CADENCE] econ boost refresh failed")
            return driver.tick(now)

    # resolver も返す: market state loop (orchestrator runtime 側) が同じ resolver に
    # state boost を書けるよう共有する (code review High#2)。
    return _CadenceDriver(), resolver


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
    _api = "on" if config.api.enabled else "off"
    _pp = config.paper_provider
    _logger.info(
        "[SYSTEM] Startup — mode=%s daemon=%s instruments=%dtrade/%dwatch "
        "API=%s provider=%s",
        config.mode, args.daemon,
        _trade_n, _watch_n,
        _api, _pp,
    )

    if not startup_checks(config):
        _logger.error("[SYSTEM] Startup aborted — startup_checks failed")
        sys.exit(1)

    is_fresh_start = not config.prices_db_path.exists()

    store = VectorStore(config.rag_db_path)
    price_store = PriceStore(config.prices_db_path)
    analysis_store = AnalysisStore(config.prices_db_path)
    hold_store = HoldDecisionStore(config.prices_db_path)
    price_provider = PriceProvider(config)

    # BridgeHealthGate: bridge プリフライト + halt 連携の単一ゲート。
    # 各 cycle (tech / 取引 / price_monitor) 冒頭で probe を呼び、health 連続2回失敗で
    # soft halt 発動。balance 同期 (live モード時のみ) も同経路に集約。
    from src.notifications.notifier import create_notifier as _create_notifier
    from src.trading.bridge_health_gate import BridgeHealthGate
    bridge_gate = BridgeHealthGate(
        config=config,
        notifier=_create_notifier(config.notifier.enabled),
        log_path=config.state_dir.parent / "logs" / "bridge_health.jsonl",
    )

    if config.paper_provider == "twelvedata":
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

    # technical 収集は trade/watch 別 interval (既定は両方 1h = 毎時)。
    # 単一 slot skip を避けるため union 時刻 + watch→trade 逐次ディスパッチにする。
    _trade_tech_set = set(effective_trade_times(
        config.schedule.technical_trade_interval_hours,
        config.schedule.technical_trade_interval_minutes,
    ))
    _watch_tech_set = set(technical_times_for(config.schedule.technical_watch_interval_hours))

    def _run_watch_tech(_t: str) -> None:
        run_watch_technical_collection(
            config, store, price_store, analysis_store,
            price_provider=price_provider,
        )

    def _run_trade_tech(_t: str) -> None:
        run_trade_technical_collection(
            config, store, price_store, analysis_store,
            price_provider=price_provider, gate=bridge_gate,
        )

    _tech_union_times, _tech_dispatch = build_technical_dispatch(
        _trade_tech_set, _watch_tech_set, _run_watch_tech, _run_trade_tech,
    )

    # cadence resolver による可変 interval 収集 (§5.3/§5.6, Phase1 Task B)。
    # enabled 時のみ driver を組む。既存収集は銘柄一括 (per-pair でない) ため、driver も
    # batch 粒度で回す: trade batch / watch batch を 1 つの論理 pair として扱い、trade batch
    # の有効 interval は trade 全 pair の最短 boost で律速する (_build_cadence_driver 参照)。
    _cadence_driver = None
    _cadence_resolver = None
    if config.schedule.cadence_enabled:
        _cadence_driver, _cadence_resolver = _build_cadence_driver(
            config, _run_watch_tech, _run_trade_tech,
        )

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
        # stage >= protect_shadow なら同じ orchestrator DB (prices_db_path) に
        # 判定を記録する store を注入する (spec §5.3, review H-c)。bootstrap worker と
        # 同一 DB に書くため別インスタンスでも records は 1 箇所に集約される。
        stage = config.orchestrator.tick_migration_stage
        if stage in ("protect_shadow", "protect_live"):
            from src.data.orchestrator_store import OrchestratorStore
            _prot_store = OrchestratorStore(config.prices_db_path)
        else:
            _prot_store = None
        monitor_interval = config.price_monitor.interval_minutes
        monitor_times = [
            f"{h:02d}:{m:02d}"
            for h in range(24)
            for m in range(0, 60, monitor_interval)
        ]
        for t in monitor_times:
            schedule.every().day.at(t, tz).do(
                _run_with_guard, _guards["price_monitor"],
                run_price_monitor, config, price_provider, bridge_gate,
                decision_store=_prot_store, protection_mode=stage,
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

    # 4. テクニカル分析。cadence_enabled なら可変 interval driver、そうでなければ
    #    現行の union 時刻 dispatch (後方互換・ロールバック先)。
    if _cadence_driver is not None:
        # driver tick を毎分 technical guard 経由で回す。guard busy (前回の技術収集が
        # まだ実行中) の間は tick 関数自体が呼ばれず、両 batch pair の last_run が凍結される。
        # 次 tick で due 判定が再び真になり自然に backfill される (定期周期を待たない)。
        def _cadence_tick() -> None:
            _cadence_driver.tick()
        schedule.every(1).minutes.do(
            _run_with_guard, _guards["technical"], _cadence_tick,
        )
        _logger.info("[CADENCE] variable-interval driver enabled (union dispatch bypassed)")
    else:
        for t in _tech_union_times:
            schedule.every().day.at(t, news_tz).do(
                _run_with_guard, _guards["technical"], _tech_dispatch, t,
            )

    # 6. 取引判定（LLMあり・指定時刻のみ）
    for t in run_times:
        schedule.every().day.at(t, tz).do(
            _run_with_slot,
            run_trading_cycle, config, store, price_store, analysis_store, hold_store,
            price_provider=price_provider,
            gate=bridge_gate,
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

    # 経済指標影響分析 (LLM 使用 → slot 経由。collector から分離、spec §2.K)
    # market_aware は付けない: 金曜イベントの actual 確定が休場後になり得るため週末も回す。
    # 10 分間隔: 検索窓が最終成功時刻から動的拡大 (econ_impact_job) するため slot busy の
    # 連続 skip でも取りこぼさない。
    if config.economic_calendar.enabled:
        from src.jobs.econ_impact_job import run_econ_impact_collection
        schedule.every(10).minutes.do(
            _run_with_slot, run_econ_impact_collection,
            config, store, price_store, analysis_store,
        )

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
                _run_weekly(config)

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

    # REST API サーバー（有効時のみ — Initial collection 前に起動）
    if config.api.enabled:
        from src.api.server import start_api_server
        # gate spec F-5: API から plan gate を操作する store。orchestrator 有効時のみ
        # 生成 (無効時に不要な DB を作らない — 実装後レビュー Low-Med)。engine は
        # _get_engine が db_path 単位で共有するため runtime 側と実体は同一。
        start_api_server(config, store, analysis_store, _llm_slot,
                         price_store, hold_store,
                         _api_orchestrator_store(config))

    # 起動直後にニュース収集+テクニカル分析を1回実行
    # prices.db が存在しない場合（初回起動）は市場時間を無視して強制実行
    _console.print(Rule("[dim]Initial collection[/dim]", style="dim"))

    # 休場中起動の明示表示 (同時に MarketStateTracker の初期状態ログも発行される)
    if market_skip_check():
        _console.print(
            "[yellow]Market is currently closed.[/yellow] "
            "[dim]Price-dependent jobs (price_monitor / exit_check / "
            "technical / trading) will stay paused until market open.[/dim]"
        )

    if args.skip_news:
        _console.print("[dim]--skip-news: 初回ニュース取得をスキップ[/dim]")
    else:
        run_news_collection(config, store)
    if args.skip_tech:
        _console.print("[dim]--skip-tech: 初回テクニカル収集をスキップ[/dim]")
    else:
        # cold start の相関欠損を避けるため watch → trade の順で逐次実行
        run_watch_technical_collection(
            config, store, price_store, analysis_store,
            force=is_fresh_start, price_provider=price_provider,
        )
        run_trade_technical_collection(
            config, store, price_store, analysis_store,
            force=is_fresh_start, price_provider=price_provider, gate=bridge_gate,
        )

    # econ impact 起動時 1 回 (spec §2.K — 旧 trade collect 内実行との空白を作らない)
    if config.economic_calendar.enabled:
        from src.jobs.econ_impact_job import run_econ_impact_collection
        run_econ_impact_collection(config, store, price_store, analysis_store)

    # スケジューラをバックグラウンドスレッドで起動
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    scheduler_thread.start()

    # Orchestrator agent loop (shadow)。enabled 時のみ planning/watch/hindsight ループ +
    # shadow 通知 worker を起動する。既存 trading cycle とは並走 (Phase 2〜6 は停止しない)。
    # broker adapter は渡さない (shadow 境界)。disabled なら None で no-op。
    # shadow 機能の結線/起動失敗で既存 scheduler/trading/API まで巻き込まないよう guard。
    # 段階導入: orchestrator が立ち上がらなくても本体は継続する (Codex Medium)。
    orchestrator = None
    try:
        from src.orchestrator.bootstrap import build_orchestrator_runtime
        orchestrator = build_orchestrator_runtime(
            config, store=store, price_store=price_store,
            analysis_store=analysis_store, price_provider=price_provider,
            # cadence_enabled 時は同じ resolver を共有し、market state boost (経路②) を
            # 実収集 interval に反映させる (code review High#2)。None なら縮退 (regime のみ)。
            cadence_resolver=_cadence_resolver,
        )
        if orchestrator is not None:
            orchestrator.start()
            _console.print(
                f"[green][OK][/green]  Orchestrator (shadow) running — "
                f"mode={config.orchestrator.mode}"
            )
    except Exception:
        _logger.exception("[ORCH] bootstrap failed — orchestrator disabled, app continues")
        _console.print(
            "[yellow][WARN][/yellow] Orchestrator bootstrap failed — disabled "
            "(既存 trading cycle は継続)"
        )
        orchestrator = None

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
            run_commands(config, store, analysis_store, _stop, _llm_slot, price_store, hold_store, price_provider=price_provider)
    finally:
        _stop.set()
        if orchestrator is not None:
            orchestrator.stop()  # ループ停止 + 通知 worker drain
        _logger.info("[SYSTEM] Shutdown — FX Paper Trader stopped")


if __name__ == "__main__":
    main()
