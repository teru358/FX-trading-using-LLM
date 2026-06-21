"""テクニカル分析スナップショットの収集ジョブ。

OHLCVデータ取得 → テクニカル指標計算 → LLM分析 → スナップショット保存。

trade/watch 別経路 (Task 6.2):
  - ``collect_watch_technical``: 監視専用銘柄のみ収集 (macro/相関なし)。
  - ``collect_trade_technical``: 取引対象を収集 (watch snapshot からの macro +
    watch 価格を PriceStore 再ロードした相関 + 経済指標影響分析)。
  - ``collect_all_technical``: watch → trade を順に回す後方互換 wrapper。
別スケジュール起動点で回せるよう watch は低頻度固定・trade のみ将来 cadence boost 対象。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from src.analysis.price_analyzer import analyze_price_action
from src.config import AppConfig, InstrumentConfig
from src.data.analysis_store import AnalysisStore
from src.data.correlation import (
    PairCorrelation,
    compute_correlations,
    format_correlation_context,
    _DEFAULT_ROLLING_WINDOW,
)
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
from src.trading.market_hours import is_market_open

if TYPE_CHECKING:
    from src.data.price_fetcher import PriceData
    from src.trading.bridge_health_gate import BridgeHealthGate

logger = logging.getLogger(__name__)


_MAX_STALENESS_FX = timedelta(hours=6)        # FX: 24時間取引
_MAX_STALENESS_WATCH = timedelta(hours=120)   # ETF/指数: 週末+米国3連休を跨ぐため5日


def _max_staleness_for(inst: InstrumentConfig) -> timedelta:
    """銘柄タイプ別の stale 閾値を返す。"""
    return _MAX_STALENESS_FX if inst.asset_type == "fx" else _MAX_STALENESS_WATCH


def _fetch_instrument_ohlcv(
    inst: InstrumentConfig,
    config: AppConfig,
    price_store: PriceStore,
    price_provider: "PriceProvider | None",
):
    """price_provider または fetch_ohlcv で OHLCV を取得する。"""
    period = f"{config.trading.lookback_days}d"
    interval = config.trading.ohlcv_interval
    if price_provider:
        return price_provider.get_ohlcv(
            inst.symbol, period=period, interval=interval, price_store=price_store,
        )
    from src.data.price_fetcher import fetch_ohlcv
    return fetch_ohlcv(
        inst.symbol, period=period, interval=interval, price_store=price_store,
    )


def _is_price_data_stale(
    price_data,
    max_staleness: timedelta = _MAX_STALENESS_FX,
) -> timedelta | None:
    """最新バーの鮮度をチェック。古すぎる場合は経過時間を返す (スキップ判定用)。"""
    from src.utils.clock import db_now

    latest_bar = price_data.df.index[-1]
    if hasattr(latest_bar, "to_pydatetime"):
        latest_bar = latest_bar.to_pydatetime()
    if hasattr(latest_bar, "tzinfo") and latest_bar.tzinfo is not None:
        latest_bar = latest_bar.replace(tzinfo=None)
    staleness = db_now() - latest_bar
    if staleness > max_staleness:
        return staleness
    return None


_CORR_LOOKBACK_DAYS = 30  # 相関に十分なバー数を確保する lookback


def _reload_watch_prices(
    config: AppConfig,
    price_store: PriceStore,
) -> dict[str, "PriceData"]:
    """相関計算用に watch 価格を PriceStore から再ロードする。

    空・バー不足・stale な watch symbol は除外する (相関入力から外れるだけで trade 収集は継続)。
    分割後は watch 収集が停止しても prices.db に古いバーが残るため、stale 判定で古いバーを
    相関から弾く。
    """
    from src.data.price_fetcher import PriceData
    from src.utils.clock import db_now

    min_bars = _DEFAULT_ROLLING_WINDOW + 5
    end = db_now()
    start = end - timedelta(days=_CORR_LOOKBACK_DAYS)
    out: dict[str, PriceData] = {}
    for w in config.watch_only_instruments:
        try:
            df = price_store.load_ohlcv(w.symbol, start, end)
        except Exception as e:
            logger.debug(f"[CORR] watch reload failed {w.symbol}: {e}")
            continue
        if df is None or df.empty or len(df) < min_bars:
            continue
        pd_w = PriceData(
            symbol=w.symbol, df=df,
            current_price=float(df["Close"].iloc[-1]), fetched_at=end,
        )
        if _is_price_data_stale(pd_w, max_staleness=_max_staleness_for(w)) is not None:
            logger.debug(f"[CORR] watch {w.symbol} stale, excluded from correlation")
            continue
        out[w.symbol] = pd_w
    return out


def _compute_and_log_tech_score(inst: InstrumentConfig, summary, config: AppConfig):
    """単一 TF (短期) の tech_score を計算。MTF 未使用時のフォールバック経路。"""
    from src.signals.technical_scorer import compute_technical_score

    tech_score = compute_technical_score(
        summary,
        indicator_cfg=config.analysis.indicators,
        pattern_cfg=config.analysis.chart_patterns,
    )
    logger.info(
        f"[COLLECT] {inst.display_name}: tech_score={tech_score.total_score:+.3f} "
        f"conf={tech_score.confidence:.2f} dir={tech_score.direction} "
        f"(SMA={tech_score.sma_score:+.2f} RSI={tech_score.rsi_score:+.2f} "
        f"MACD={tech_score.macd_score:+.2f} ICH={tech_score.ichimoku_score:+.2f} "
        f"BB={tech_score.bb_score:+.2f} PAT={tech_score.pattern_score:+.2f} ADX×{tech_score.adx_factor:.1f})"
    )
    return tech_score


def _compute_mtf_and_log(inst: InstrumentConfig, df_1h, config: AppConfig):
    """MTF 版: 各 TF の summary と合成 TechnicalScore を計算してログに残す。

    戻り値: (summaries dict, multi_tf_score, short_summary)
    short_summary は既存の compute_indicators(df_1h, full) の結果で、
    LLM プロンプト用 formatted_data の元データとして使う。
    """
    from src.data.mtf import compute_mtf_summaries
    from src.signals.technical_scorer import (
        compute_multi_tf_technical_score,
        compute_technical_score,
    )

    mtf_cfg = config.analysis.multi_timeframe
    timeframes = {
        "long": {
            "lookback_days": mtf_cfg.long.lookback_days,
            "interval": mtf_cfg.long.interval,
            "enabled": mtf_cfg.long.enabled,
        },
        "medium": {
            "lookback_days": mtf_cfg.medium.lookback_days,
            "interval": mtf_cfg.medium.interval,
            "enabled": mtf_cfg.medium.enabled,
        },
        "short": {
            "lookback_days": mtf_cfg.short.lookback_days,
            "interval": mtf_cfg.short.interval,
            "enabled": mtf_cfg.short.enabled,
        },
    }
    summaries = compute_mtf_summaries(df_1h, config.analysis, timeframes)

    # 各 TF に適用する indicator/pattern cfg は mtf 内部でフィルタされているが、
    # compute_technical_score にも同じ filtered cfg を渡して disabled 指標の
    # 疑似 bearish を防ぐ
    from src.data.mtf import filter_indicator_cfg_for_tf, filter_pattern_cfg_for_tf

    tf_subset_map = {"long": "regime", "medium": "structure", "short": "full"}
    tf_scores = {}
    for tf_name, summary in summaries.items():
        subset = tf_subset_map[tf_name]
        filtered_ind = filter_indicator_cfg_for_tf(config.analysis.indicators, subset)
        filtered_pat = filter_pattern_cfg_for_tf(config.analysis.chart_patterns, subset)
        tf_scores[tf_name] = compute_technical_score(
            summary,
            indicator_cfg=filtered_ind,
            pattern_cfg=filtered_pat,
        )

    mtf_score = compute_multi_tf_technical_score(tf_scores, mtf_cfg.weights)

    # ログ: 各 TF + 合成結果
    tf_log_parts = [
        f"{name}={tf_scores[name].total_score:+.2f}({tf_scores[name].direction[:1].upper()})"
        for name in ("long", "medium", "short") if name in tf_scores
    ]
    logger.info(
        f"[COLLECT] {inst.display_name}: MTF tech_score={mtf_score.total_score:+.3f} "
        f"conf={mtf_score.confidence:.2f} dir={mtf_score.direction} "
        f"align={mtf_score.alignment:.2f} | {' '.join(tf_log_parts)}"
    )

    short_summary = summaries.get("short")
    return summaries, mtf_score, short_summary


def _build_rag_contexts(
    inst: InstrumentConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    config: AppConfig,
) -> tuple[str, str, str]:
    """LLM プロンプト向けの (news_ctx, refl_ctx (+insights), prev_ctx) を構築する。"""
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

    # 過去の ask 洞察を reflection 末尾に結合
    insights = store.get_recent_insights(pair=inst.symbol, limit=3, lookback_hours=72)
    insight_ctx = format_insights_for_prompt(insights)
    if insight_ctx:
        refl_ctx = f"{refl_ctx}\n\n{insight_ctx}" if refl_ctx else insight_ctx

    prev_snapshots = analysis_store.get_recent_ok_snapshots(inst.symbol, hours=8)
    prev_ctx = format_previous_analysis_for_prompt(prev_snapshots[0] if prev_snapshots else None)
    return news_ctx, refl_ctx, prev_ctx


def _combine_macro(macro_context: str, correlation_context: str) -> str:
    """macro と correlation を単一テキストに結合する。空文字列の扱いに注意。"""
    if not correlation_context:
        return macro_context
    if not macro_context:
        return correlation_context
    return f"{macro_context}\n\n{correlation_context}"


def _compute_summary_and_score(inst: InstrumentConfig, price_data, config: AppConfig):
    """インジケータ計算 + テクニカルスコア算出を MTF/単一 TF 分岐込みで一括処理する。

    戻り値: (summary, tech_score, mtf_score)
      - summary: LLM プロンプトに渡す短期 TF の IndicatorSummary
      - tech_score: TechnicalScore (MTF 時は合成結果を as_technical_score() で変換)
      - mtf_score: MultiTfTechnicalScore (単一 TF 時は None)
    """
    mtf_cfg = config.analysis.multi_timeframe
    if not mtf_cfg.enabled:
        # 従来の単一 TF 動作
        _, summary = compute_indicators(
            price_data.df,
            indicator_cfg=config.analysis.indicators,
            pattern_cfg=config.analysis.chart_patterns,
        )
        tech_score = _compute_and_log_tech_score(inst, summary, config)
        return summary, tech_score, None

    # MTF: 各 TF を計算 → 合成 TechnicalScore
    _, mtf_score, short_summary = _compute_mtf_and_log(inst, price_data.df, config)
    if short_summary is not None:
        # MTF 合成結果を既存インターフェース互換形に変換
        return short_summary, mtf_score.as_technical_score(), mtf_score

    # short TF がリサンプル失敗 → 単一 TF にフォールバック
    logger.warning(
        f"[COLLECT] {inst.display_name}: MTF short TF unavailable, falling back to single TF"
    )
    _, summary = compute_indicators(
        price_data.df,
        indicator_cfg=config.analysis.indicators,
        pattern_cfg=config.analysis.chart_patterns,
    )
    tech_score = _compute_and_log_tech_score(inst, summary, config)
    return summary, tech_score, mtf_score


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
    price_data: "PriceData | None" = None,
) -> None:
    """1銘柄のOHLCVを取得してテクニカル分析を実行し、スナップショットを保存する。

    全経路で必ず 1 行 (ok / stale_price / failed) を analysis_store に書く。
    上位は本関数を try/except する必要は無い (内部で全例外を sentinel 化)。

    price_data が渡された場合は内部フェッチをスキップする (prefetch キャッシュ経由)。
    """
    # Phase 1: OHLCV 取得 (prefetch されていなければここで取得)
    if price_data is None:
        price_data = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)

    # Phase 2: 鮮度チェック (古ければ stale_price sentinel + skip)
    staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst))
    if staleness is not None:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="stale_price",
            reason=f"latest bar {staleness} ago (max {_max_staleness_for(inst)})",
        )
        logger.info(
            f"[COLLECT] {inst.display_name}: stale_price sentinel ({staleness} ago)"
        )
        return

    # Phase 3: インジケータ + tech_score (失敗 → failed sentinel)
    try:
        summary, tech_score, mtf_score = _compute_summary_and_score(inst, price_data, config)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"indicator_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (indicator) — {e}",
            exc_info=True,
        )
        return

    # Phase 4: RAG コンテキスト構築 (失敗 → failed sentinel)
    try:
        news_ctx, refl_ctx, prev_ctx = _build_rag_contexts(inst, store, analysis_store, config)
        full_macro = _combine_macro(macro_context, correlation_context)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"rag_context_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (rag context) — {e}",
            exc_info=True,
        )
        return

    # Phase 5: LLM 分析 (失敗 → failed sentinel)
    try:
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
            mtf_score=mtf_score,
        )
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"llm_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (llm) — {e}",
            exc_info=True,
        )
        return

    # Phase 6: 成功 → ok 行保存
    analysis_store.add_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )


async def collect_watch_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """watch_only 銘柄のみのテクニカル分析を収集する (macro/correlation 無し)。"""
    if not force and not is_market_open():
        return

    watch_only = config.watch_only_instruments
    if not watch_only:
        return

    llm_price = create_llm_client(config, "price_analysis")
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(f"[COLLECT] Watch technical: {len(watch_only)} watch-only instruments")

    for i, inst in enumerate(watch_only):
        try:
            price_data = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)
        except Exception as e:
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed",
                reason=f"prefetch_failed: {type(e).__name__}: {e}",
            )
            logger.warning(f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)")
            if i < len(watch_only) - 1:
                await asyncio.sleep(delay)
            continue
        try:
            await _collect_one(
                inst, config, store, price_store, analysis_store, llm_price,
                price_provider=price_provider, price_data=price_data,
            )
        except Exception as e:
            logger.error(
                f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}",
                exc_info=True,
            )
            try:
                analysis_store.add_sentinel(
                    symbol=inst.symbol, status="failed",
                    reason=f"unexpected_raise: {type(e).__name__}: {e}",
                )
            except Exception as sentinel_err:
                logger.error(
                    f"[COLLECT] {inst.display_name}: sentinel write also failed: "
                    f"{type(sentinel_err).__name__}: {sentinel_err}",
                    exc_info=False,
                )
        if i < len(watch_only) - 1:
            await asyncio.sleep(delay)


async def _collect_econ_impact(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    tradeable: list[InstrumentConfig],
) -> None:
    """経済指標影響分析 (オプション)。現 collect_all_technical Phase 3 をそのまま移植。"""
    if not config.economic_calendar.enabled:
        return
    try:
        from src.utils.clock import db_now

        from src.data.econ_event_store import EconEventStore
        from src.jobs.econ_calendar_fetcher import refresh_recent_events
        from src.analysis.econ_impact_analyzer import (
            analyze_event_impact, PairReaction, SnapshotBrief
        )
        from src.analysis.economic_calendar import classify_surprise
        from src.rag.embedder import make_embed_fn

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

                        snaps = analysis_store.get_recent_ok_snapshots(p.symbol, hours=2)
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
                    embed_fn = make_embed_fn(config)
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
                        analyzed_at=db_now(),
                    )
                    econ_store.mark_analyzed(ev.event_id)
                    logger.info(f"[ECON] Analyzed {ev.event_id}: {ev.title[:40]}")
                except Exception as e:
                    logger.error(f"[ECON] Analysis failed for {ev.event_id}: {e}", exc_info=True)
    except Exception as e:
        logger.warning(f"[ECON] Economic calendar phase failed: {e}")


async def collect_trade_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """tradeable 銘柄のテクニカル分析を収集する (macro + 相関 + econ 付き)。"""
    if not force and not is_market_open():
        return

    tradeable = config.tradeable_instruments
    watch_only = config.watch_only_instruments
    if not tradeable:
        return

    llm_price = create_llm_client(config, "price_analysis")
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(f"[COLLECT] Trade technical: {len(tradeable)} tradeable instruments")

    prices: dict[str, "PriceData"] = {}
    prefetch_errors: dict[str, str] = {}
    for inst in tradeable:
        try:
            prices[inst.symbol] = _fetch_instrument_ohlcv(
                inst, config, price_store, price_provider,
            )
        except Exception as e:
            prefetch_errors[inst.symbol] = f"{type(e).__name__}: {e}"
            logger.warning(f"[PREFETCH] {inst.display_name}: OHLCV fetch failed: {e}")

    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_ok_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    macro_ctx = format_macro_context_for_prompt(
        macro_snapshots, watch_only, realtime_provider=config.paper_provider,
    )

    correlations: list[PairCorrelation] = []
    if watch_only:
        try:
            watch_prices = _reload_watch_prices(config, price_store)
            trade_prices = {i.symbol: prices[i.symbol] for i in tradeable if i.symbol in prices}
            watch_names = {inst.symbol: inst.display_name for inst in watch_only}
            correlations = compute_correlations(trade_prices, watch_prices, watch_names)
            logger.info(f"[CORR] Computed {len(correlations)} correlation pairs")
        except Exception as e:
            logger.error(f"[CORR] Correlation computation failed: {e}", exc_info=True)

    for i, inst in enumerate(tradeable):
        pd_cached = prices.get(inst.symbol)
        if pd_cached is None:
            err = prefetch_errors.get(inst.symbol, "no cached price (unknown reason)")
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed", reason=f"prefetch_failed: {err}",
            )
            logger.warning(f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)")
            if i < len(tradeable) - 1:
                await asyncio.sleep(delay)
            continue
        try:
            corr_ctx = format_correlation_context(correlations, inst.symbol)
            await _collect_one(
                inst, config, store, price_store, analysis_store, llm_price,
                macro_context=macro_ctx, correlation_context=corr_ctx,
                price_provider=price_provider, price_data=pd_cached,
            )
        except Exception as e:
            logger.error(
                f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}",
                exc_info=True,
            )
            try:
                analysis_store.add_sentinel(
                    symbol=inst.symbol, status="failed",
                    reason=f"unexpected_raise: {type(e).__name__}: {e}",
                )
            except Exception as sentinel_err:
                logger.error(
                    f"[COLLECT] {inst.display_name}: sentinel write also failed: "
                    f"{type(sentinel_err).__name__}: {sentinel_err}",
                    exc_info=False,
                )
        if i < len(tradeable) - 1:
            await asyncio.sleep(delay)

    await _collect_econ_impact(config, store, price_store, analysis_store, tradeable)
    logger.info("=== Trade technical collection complete ===")


async def collect_all_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """全有効銘柄のテクニカル分析を収集する (後方互換 wrapper)。

    watch → trade の順に収集する。trade 経路は watch の保存済み価格を相関に使うため、
    1 回の実行では watch を先に回す (初回 cold start でも相関が成立する)。
    """
    if not force and not is_market_open():
        return
    await collect_watch_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    )
    await collect_trade_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    )
    logger.info("=== Technical collection complete ===")


def run_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """schedule ライブラリから呼び出す同期ラッパー。

    gate が渡されたら冒頭で probe する (sync_balance=True、毎時の balance 更新)。
    """
    if gate is not None:
        gate.probe(caller="tech", sync_balance=True)
    asyncio.run(collect_all_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    ))


def run_trade_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """trade 経路の同期ラッパー。gate probe (balance 同期) は trade 文脈で行う。"""
    if gate is not None:
        gate.probe(caller="tech", sync_balance=True)
    asyncio.run(collect_trade_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    ))


def run_watch_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """watch 経路の同期ラッパー。gate probe はしない (発注に関与しないため)。"""
    asyncio.run(collect_watch_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    ))
