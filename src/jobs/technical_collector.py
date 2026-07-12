"""テクニカル分析スナップショットの収集ジョブ。

OHLCV取得 → テクニカル指標計算 → 決定的スコア算出 → スナップショット保存
(LLM 不使用)。

trade/watch 別経路 (Task 6.2):
  - ``collect_watch_technical``: 監視専用銘柄のみ収集。
  - ``collect_trade_technical``: 取引対象を収集 (経済指標影響分析付き。
    macro/相関は LLM 廃止に伴い除去)。
  - ``collect_all_technical``: watch → trade を順に回す後方互換 wrapper。
watch/trade は別 public wrapper を持ち、main では union 時刻の単一 dispatch で
watch→trade 順に実行する (`build_technical_dispatch`)。watch は base interval 固定、
trade のみ将来 cadence boost 対象 (§5.3)。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from src.config import AppConfig, InstrumentConfig
from src.data.analysis_store import AnalysisStore
from src.data.indicators import compute_indicators
from src.data.price_provider import PriceProvider
from src.data.price_store import PriceStore
from src.rag.vector_store import VectorStore
from src.trading.market_hours import is_market_open
from src.utils.clock import to_db_naive_datetime

if TYPE_CHECKING:
    from src.data.price_fetcher import PriceData
    from src.trading.bridge_health_gate import BridgeHealthGate

logger = logging.getLogger(__name__)


# FX 側の閾値は config 化済み (ScheduleConfig.technical_max_staleness_fx_minutes)。
_MAX_STALENESS_WATCH = timedelta(hours=120)   # ETF/指数: 週末+米国3連休を跨ぐため5日


def _max_staleness_for(inst: InstrumentConfig, config: AppConfig) -> timedelta:
    """銘柄タイプ別の stale 閾値を返す (FX は config、watch は定数)。"""
    if inst.asset_type == "fx":
        return timedelta(minutes=config.schedule.technical_max_staleness_fx_minutes)
    return _MAX_STALENESS_WATCH


def _ohlcv_interval_for(inst: InstrumentConfig, config: AppConfig) -> str:
    """収集基底足を銘柄種別で決める。

    trade 銘柄は config (day=15m)。watch 銘柄は 1h 固定 — spec 2026-07-05 §2
    non-goal (watch 経路は現行のまま)。yfinance は 15m を period>60d で拒否する
    ため、watch を 15m に載せると収集が壊れる。
    """
    if inst.is_tradeable:
        return config.trading.ohlcv_interval
    return "1h"


def _fetch_instrument_ohlcv(
    inst: InstrumentConfig,
    config: AppConfig,
    price_store: PriceStore,
    price_provider: "PriceProvider | None",
):
    """price_provider または fetch_ohlcv で OHLCV を取得する。"""
    period = f"{config.trading.lookback_days}d"
    interval = _ohlcv_interval_for(inst, config)
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
    max_staleness: timedelta,
) -> timedelta | None:
    """最新バーの鮮度をチェック。古すぎる場合は経過時間を返す (スキップ判定用)。"""
    from src.utils.clock import db_now, to_db_naive_datetime

    latest_bar = price_data.df.index[-1]
    if hasattr(latest_bar, "to_pydatetime"):
        latest_bar = latest_bar.to_pydatetime()
    # aware は DB 規約 (naive local) に変換してから比較する (naive UTC のまま剥がすと
    # JST 環境で +9h ズレ、新鮮なバーが誤って stale_price 判定される)。
    latest_bar = to_db_naive_datetime(latest_bar)
    staleness = db_now() - latest_bar
    if staleness > max_staleness:
        return staleness
    return None


def _econ_window_for(event_time: datetime, hours: int = 1) -> tuple[datetime, datetime]:
    """econ event_time (aware UTC) を DB 規約 (naive local) に変換し ±hours の窓を返す。

    ohlcv テーブルは naive machine-local なので、load_ohlcv の窓も local 時刻で指定
    する。naive UTC のまま渡すと窓がズレてバーを取りこぼす。
    """
    base = to_db_naive_datetime(event_time)
    return base - timedelta(hours=hours), base + timedelta(hours=hours)


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
    summaries = compute_mtf_summaries(
        df_1h, config.analysis, timeframes,
        base_interval=_ohlcv_interval_for(inst, config),
    )

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
    return summary, tech_score, None


def _build_snapshot_data(
    *, pair, analyzed_at, tech_score, mtf_score, chart_patterns,
) -> "TechnicalSnapshotData":
    """決定的スコアから TechnicalSnapshotData を組む (spec §2.A)。"""
    from src.analysis.technical_snapshot_data import TechnicalSnapshotData

    components = {
        "sma": round(tech_score.sma_score, 4),
        "rsi": round(tech_score.rsi_score, 4),
        "macd": round(tech_score.macd_score, 4),
        "ichimoku": round(tech_score.ichimoku_score, 4),
        "bb": round(tech_score.bb_score, 4),
        "pattern": round(tech_score.pattern_score, 4),
        "adx_factor": round(tech_score.adx_factor, 4),
    }
    if mtf_score is not None:
        mtf_alignment = mtf_score.alignment
        tf_scores = {
            name: {"score": round(s.total_score, 4), "direction": s.direction}
            for name, s in mtf_score.tf_scores.items()
        }
    else:
        mtf_alignment = None
        tf_scores = {}
    return TechnicalSnapshotData(
        pair=pair,
        analyzed_at=analyzed_at,
        bias_score=tech_score.total_score,
        confidence=tech_score.confidence,
        direction_bias=tech_score.direction,
        mtf_alignment=mtf_alignment,
        tf_scores=tf_scores,
        components=components,
        patterns=list(chart_patterns or []),
    )


async def _collect_one(
    inst: InstrumentConfig,
    config: AppConfig,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    price_provider: "PriceProvider | None" = None,
    price_data: "PriceData | None" = None,
) -> None:
    """1銘柄のOHLCVを取得して決定的テクニカル分析を行い snapshot を保存する。

    全経路で必ず 1 行 (ok / stale_price / failed) を analysis_store に書く。
    上位は本関数を try/except する必要は無い (内部で全例外を sentinel 化)。

    price_data が渡された場合は内部フェッチをスキップする (prefetch キャッシュ経由)。
    """
    from src.utils.clock import db_now

    # Phase 1: OHLCV 取得 (prefetch されていなければここで取得)
    if price_data is None:
        price_data = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)

    # Phase 2: 鮮度チェック (古ければ stale_price sentinel + skip)
    staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst, config))
    if staleness is not None:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="stale_price",
            reason=f"latest bar {staleness} ago (max {_max_staleness_for(inst, config)})",
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

    # Phase 4: 決定的スナップショット組み立て + 保存
    data = _build_snapshot_data(
        pair=inst.symbol,
        analyzed_at=db_now(),
        tech_score=tech_score,
        mtf_score=mtf_score,
        chart_patterns=summary.chart_patterns,
    )
    analysis_store.add_snapshot(data)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={data.bias_score:+.2f} conf={data.confidence:.2f} dir={data.direction_bias}"
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

    delay = config.schedule.technical_inter_pair_delay_seconds
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
                inst, config, price_store, analysis_store,
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


async def collect_trade_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """tradeable 銘柄のテクニカル分析を収集する (決定的スコア + econ 付き)。"""
    if not force and not is_market_open():
        return

    tradeable = config.tradeable_instruments
    if not tradeable:
        return

    delay = config.schedule.technical_inter_pair_delay_seconds
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
            await _collect_one(
                inst, config, price_store, analysis_store,
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
