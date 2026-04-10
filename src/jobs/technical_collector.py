"""テクニカル分析スナップショットの収集ジョブ。

OHLCVデータ取得 → テクニカル指標計算 → LLM分析 → スナップショット保存。

2フェーズ実行:
  Phase 1: 監視専用銘柄（指数・参照FX）を先に収集
  Phase 2: 取引対象FXペアを収集（Phase 1 のマクロコンテキスト付き）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from src.analysis.price_analyzer import analyze_price_action
from src.config import AppConfig, InstrumentConfig
from src.data.analysis_store import AnalysisStore
from src.data.correlation import PairCorrelation, compute_correlations, format_correlation_context
from src.data.indicators import compute_indicators
from src.data.price_provider import PriceProvider
from src.data.price_store import PriceStore
from src.llm.client import LLMClient
from src.llm.factory import create_llm_client
from src.rag.prompt_formatter import (
    format_insights_for_prompt,
    format_macro_context_for_prompt,
    format_news_for_prompt,
    format_previous_analysis_for_prompt,
    format_reflections_for_prompt,
)
from src.rag.vector_store import VectorStore
from src.trading.market_hours import is_market_open, market_status_label

logger = logging.getLogger(__name__)


async def _collect_one(
    inst: InstrumentConfig,
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
    macro_context: str = "",
    correlation_context: str = "",
    price_provider: "PriceProvider | None" = None,
) -> None:
    """1銘柄のOHLCVを取得してテクニカル分析を実行し、スナップショットを保存する。"""
    from datetime import datetime, timedelta

    if price_provider:
        price_data = price_provider.get_ohlcv(
            inst.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )
    else:
        from src.data.price_fetcher import fetch_ohlcv
        price_data = fetch_ohlcv(
            inst.symbol,
            period=f"{config.trading.lookback_days}d",
            interval=config.trading.ohlcv_interval,
            price_store=price_store,
        )

    # 価格データの鮮度チェック: 最新バーが古すぎる場合LLM呼び出しをスキップ
    latest_bar = price_data.df.index[-1]
    if hasattr(latest_bar, "to_pydatetime"):
        latest_bar = latest_bar.to_pydatetime()
    if hasattr(latest_bar, "tzinfo") and latest_bar.tzinfo is not None:
        latest_bar = latest_bar.replace(tzinfo=None)
    staleness = datetime.now() - latest_bar
    max_staleness = timedelta(hours=6)
    if staleness > max_staleness:
        logger.info(
            f"[COLLECT] {inst.display_name}: stale data (latest bar {staleness} ago), skipping LLM analysis"
        )
        return

    _, summary = compute_indicators(
        price_data.df,
        indicator_cfg=config.analysis.indicators,
        pattern_cfg=config.analysis.chart_patterns,
    )

    from src.signals.technical_scorer import compute_technical_score
    tech_score = compute_technical_score(summary)
    logger.info(
        f"[COLLECT] {inst.display_name}: tech_score={tech_score.total_score:+.3f} "
        f"conf={tech_score.confidence:.2f} dir={tech_score.direction} "
        f"(SMA={tech_score.sma_score:+.2f} RSI={tech_score.rsi_score:+.2f} "
        f"MACD={tech_score.macd_score:+.2f} ICH={tech_score.ichimoku_score:+.2f} "
        f"BB={tech_score.bb_score:+.2f} PAT={tech_score.pattern_score:+.2f} ADX×{tech_score.adx_factor:.1f})"
    )

    # RAGからコンテキストを構築
    news_entries = store.get_recent_category_news(
        categories=inst.news_categories,
        lookback_hours=config.rag.news_lookback_hours,
    )
    reflections = store.get_recent_reflections(
        pair=inst.symbol,
        limit=config.rag.reflection_lookback_count,
    )
    news_ctx = format_news_for_prompt(news_entries)
    refl_ctx = format_reflections_for_prompt(reflections)

    # 過去のask洞察を取得
    insights = store.get_recent_insights(pair=inst.symbol, limit=3, lookback_hours=72)
    insight_ctx = format_insights_for_prompt(insights)
    if insight_ctx:
        refl_ctx = f"{refl_ctx}\n\n{insight_ctx}" if refl_ctx else insight_ctx

    # 前回分析スナップショット（直近1件）
    prev_snapshots = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
    prev_ctx = format_previous_analysis_for_prompt(prev_snapshots[0] if prev_snapshots else None)

    # マクロコンテキストに相関データを付加
    full_macro = macro_context
    if correlation_context:
        full_macro = f"{macro_context}\n\n{correlation_context}" if macro_context else correlation_context

    price_analysis = await analyze_price_action(
        pair_cfg=inst,
        price_data=price_data,
        summary=summary,
        llm=llm,
        temperature=config.llm.price_analysis.temperature,
        news_context=news_ctx,
        reflection_context=refl_ctx,
        previous_analysis=prev_ctx,
        macro_context=full_macro,
        user_notes_path=config.user_notes_path,
        tech_score=tech_score,
    )
    analysis_store.upsert_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )


async def collect_all_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """全有効銘柄のテクニカル分析を2フェーズで収集してストアに格納する。"""
    if not force and not is_market_open():
        logger.info(f"Market {market_status_label()}. Skipping technical collection.")
        return

    llm_price = create_llm_client(config, "price_analysis")
    watch_only = config.watch_only_instruments
    tradeable = config.tradeable_instruments
    all_instruments = watch_only + tradeable
    delay = config.news_collection.inter_pair_delay_seconds

    logger.info(
        f"=== Technical collection started "
        f"({len(tradeable)} trade + {len(watch_only)} watch, {delay}s interval) | "
        f"llm={type(llm_price).__name__}({llm_price.model_name}) ==="
    )

    # Phase 1: 監視専用銘柄（指数・参照FX）を先に収集
    if watch_only:
        logger.info(f"[COLLECT] Phase 1: {len(watch_only)} watch-only instruments")
    for i, inst in enumerate(watch_only):
        try:
            logger.debug(f"[COLLECT] {inst.display_name}: starting OHLCV + technical analysis...")
            await _collect_one(inst, config, store, price_store, analysis_store, llm_price, price_provider=price_provider)
        except Exception as e:
            logger.error(f"[COLLECT] {inst.display_name}: technical analysis failed: {e}", exc_info=True)
        if i < len(watch_only) - 1:
            await asyncio.sleep(delay)

    # Phase 1 の結果からマクロコンテキストを構築
    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    macro_ctx = format_macro_context_for_prompt(
        macro_snapshots, watch_only,
        realtime_provider=config.price_provider.realtime_provider,
    )

    # Phase 1.5: trade×watch の価格相関を計算（LLMなし）
    correlations: list[PairCorrelation] = []
    if watch_only and tradeable:
        try:
            from src.data.price_fetcher import fetch_ohlcv as _fetch_ohlcv
            _period = f"{config.trading.lookback_days}d"
            _interval = config.trading.ohlcv_interval

            trade_prices = {}
            for inst in tradeable:
                try:
                    pd_ = (price_provider.get_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store)
                           if price_provider else _fetch_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store))
                    trade_prices[inst.symbol] = pd_
                except Exception as e:
                    logger.warning(f"[CORR] {inst.display_name}: OHLCV fetch failed: {e}")

            watch_prices = {}
            for inst in watch_only:
                try:
                    pd_ = (price_provider.get_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store)
                           if price_provider else _fetch_ohlcv(inst.symbol, period=_period, interval=_interval, price_store=price_store))
                    watch_prices[inst.symbol] = pd_
                except Exception as e:
                    logger.warning(f"[CORR] {inst.display_name}: OHLCV fetch failed: {e}")

            watch_names = {inst.symbol: inst.display_name for inst in watch_only}
            correlations = compute_correlations(trade_prices, watch_prices, watch_names)
            logger.info(f"[CORR] Computed {len(correlations)} correlation pairs")
        except Exception as e:
            logger.error(f"[CORR] Correlation computation failed: {e}", exc_info=True)

    # Phase 2: 取引対象FXペアを収集（マクロコンテキスト + 相関データ付き）
    if tradeable:
        if watch_only:
            await asyncio.sleep(delay)
        logger.info(f"[COLLECT] Phase 2: {len(tradeable)} tradeable instruments")
    for i, inst in enumerate(tradeable):
        try:
            corr_ctx = format_correlation_context(correlations, inst.symbol)
            logger.debug(f"[COLLECT] {inst.display_name}: starting OHLCV + technical analysis...")
            await _collect_one(inst, config, store, price_store, analysis_store, llm_price,
                               macro_context=macro_ctx, correlation_context=corr_ctx, price_provider=price_provider)
        except Exception as e:
            logger.error(f"[COLLECT] {inst.display_name}: technical analysis failed: {e}", exc_info=True)
        if i < len(tradeable) - 1:
            await asyncio.sleep(delay)

    # TradingView チャート反映（オプション・trade銘柄の最新スナップショット）
    if config.tradingview.enabled and tradeable:
        try:
            from src.tradingview.cdp_client import CDPClient
            from src.tradingview.pine_injector import PineInjector
            from src.tradingview.chart_control import to_tv_ticker
            from src.tradingview.script_generator import SignalData, generate_multi_signal_pine

            tv_cdp = CDPClient(host=config.tradingview.cdp_host, port=config.tradingview.cdp_port)
            if await tv_cdp.connect():
                try:
                    injector = PineInjector(tv_cdp)
                    sig_data_list = []
                    for inst in tradeable:
                        snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=2)
                        if not snaps:
                            continue
                        snap = snaps[0]
                        sig_data_list.append(SignalData(
                            pair=inst.display_name,
                            tv_ticker=to_tv_ticker(inst.symbol),
                            direction=snap.direction_bias,
                            confidence=snap.confidence,
                            reason=snap.reasoning_summary[:80] if snap.reasoning_summary else "",
                            bias_score=snap.bias_score,
                        ))
                    if sig_data_list:
                        pine = generate_multi_signal_pine(sig_data_list)
                        result = await injector.inject_and_compile(pine)
                        if result["success"]:
                            logger.info(f"[TV] Technical snapshots reflected ({len(sig_data_list)} pairs)")
                        else:
                            logger.warning(f"[TV] Pine compile errors: {result['errors']}")
                finally:
                    await tv_cdp.disconnect()
        except Exception as e:
            logger.warning(f"[TV] Chart visualization failed: {e}")

    # Phase 3: 経済指標影響分析 (オプション)
    if config.economic_calendar.enabled:
        try:
            from datetime import datetime
            from functools import partial

            from src.data.econ_event_store import EconEventStore
            from src.jobs.econ_calendar_fetcher import refresh_recent_events
            from src.analysis.econ_impact_analyzer import (
                analyze_event_impact, PairReaction, SnapshotBrief
            )
            from src.analysis.economic_calendar import classify_surprise
            from src.rag.embedder import embed_text
            from src.llm.factory import create_llm_client

            econ_store = EconEventStore(config.econ_db_path)

            # 3a. actual更新
            refresh_recent_events(
                econ_store,
                lookback_min=config.economic_calendar.refresh_lookback_min,
                currencies=config.economic_calendar.currencies,
            )

            # 3b. 未分析イベントを取得
            events_to_analyze = econ_store.get_unanalyzed_with_actual(
                lookback_min=config.economic_calendar.refresh_lookback_min,
                min_importance=config.economic_calendar.post_event_impact_min,
            )

            if events_to_analyze:
                llm_reflect = create_llm_client(config, "reflection")
                logger.info(f"[ECON] {len(events_to_analyze)} events to analyze")

                for ev in events_to_analyze:
                    try:
                        # 関連ペアを特定 (base または quote が該当通貨)
                        related_pairs = [
                            p for p in tradeable
                            if ev.currency in (p.base_currency, p.quote_currency)
                        ]
                        if not related_pairs:
                            econ_store.mark_analyzed(ev.event_id)
                            continue

                        # 価格反応データ収集
                        pair_reactions = []
                        snapshot_briefs = []
                        for p in related_pairs:
                            try:
                                event_time_naive = ev.event_time.replace(tzinfo=None)
                                pd_ = price_store.load_ohlcv(
                                    p.symbol,
                                    event_time_naive - timedelta(hours=1),
                                    event_time_naive + timedelta(hours=1),
                                )
                                if pd_.empty or len(pd_) < 2:
                                    continue

                                def _close_at(offset_min: int) -> float:
                                    target = event_time_naive + timedelta(minutes=offset_min)
                                    best_idx = 0
                                    best_diff = None
                                    for i, ts in enumerate(pd_.index):
                                        ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                                        if hasattr(ts_py, "tzinfo") and ts_py.tzinfo is not None:
                                            ts_py = ts_py.replace(tzinfo=None)
                                        diff = abs((ts_py - target).total_seconds())
                                        if best_diff is None or diff < best_diff:
                                            best_diff = diff
                                            best_idx = i
                                    return float(pd_["Close"].iloc[best_idx])

                                pair_reactions.append(PairReaction(
                                    pair=p.display_name,
                                    t_minus_5=_close_at(-5),
                                    t_zero=_close_at(0),
                                    t_plus_5=_close_at(5),
                                    t_plus_15=_close_at(15),
                                    t_plus_30=_close_at(30),
                                ))
                            except Exception as e:
                                logger.debug(f"[ECON] price reaction failed for {p.symbol}: {e}")

                            snaps = analysis_store.get_recent_snapshots(p.symbol, hours=2)
                            if snaps:
                                s = snaps[0]
                                snapshot_briefs.append(SnapshotBrief(
                                    pair=p.display_name,
                                    bias_score=s.bias_score,
                                    confidence=s.confidence,
                                    direction_bias=s.direction_bias,
                                ))

                        if not pair_reactions:
                            econ_store.mark_analyzed(ev.event_id)
                            continue

                        # LLM分析実行
                        report = await analyze_event_impact(
                            event=ev,
                            pair_reactions=pair_reactions,
                            snapshots=snapshot_briefs,
                            llm=llm_reflect,
                            temperature=config.llm.reflection.temperature,
                        )

                        # RAG保存
                        embed_fn = partial(
                            embed_text,
                            ollama_base_url=config.llm.ollama.base_url,
                            model=config.rag.embedding_model,
                        )
                        embedding = await embed_fn(report)
                        store.upsert_econ_analysis(
                            event_id=ev.event_id,
                            text=report,
                            embedding=embedding,
                            title=ev.title,
                            currency=ev.currency,
                            importance=ev.importance,
                            event_time=ev.event_time,
                            actual=ev.actual,
                            forecast=ev.forecast,
                            surprise=classify_surprise(ev.actual, ev.forecast),
                            analyzed_at=datetime.now(),
                        )
                        econ_store.mark_analyzed(ev.event_id)
                        logger.info(f"[ECON] Analyzed {ev.event_id}: {ev.title[:40]}")
                    except Exception as e:
                        logger.error(f"[ECON] Analysis failed for {ev.event_id}: {e}", exc_info=True)
        except Exception as e:
            logger.warning(f"[ECON] Economic calendar phase failed: {e}")

    logger.info("=== Technical collection complete ===")


def run_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。"""
    asyncio.run(collect_all_technical(config, store, price_store, analysis_store, force=force, price_provider=price_provider))
