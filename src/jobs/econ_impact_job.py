"""経済指標影響分析ジョブ (spec §2.K)。

technical collector から分離。reflection LLM を使うため slot 経由の独立ジョブとして
スケジュールされる。検索窓は最終「全件成功」時刻から動的拡大する。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta

from src.config import AppConfig, InstrumentConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.llm.factory import create_llm_client
from src.rag.vector_store import VectorStore
from src.jobs.technical_collector import _econ_window_for, _ohlcv_interval_for
from src.utils.clock import db_now, to_db_naive_datetime

logger = logging.getLogger(__name__)

_LOOKBACK_CAP_MIN = 4320  # 72h (spec §2.K: 金曜障害→月曜復帰の週末跨ぎ保護)

_status_lock = threading.Lock()
_status: dict = {
    "last_attempt": None,   # 毎実行の開始時刻
    "last_success": None,   # フェーズ完走 + failed==0 の時刻 (動的検索窓の基準)
    "pending": 0,           # 処理後に DB 再取得した未分析イベント数
    "failed": 0,            # 当該実行で失敗したイベント数
}


def get_econ_impact_status() -> dict:
    with _status_lock:
        return dict(_status)


def _dynamic_lookback_min(config) -> int:
    """最終「全件成功」からの経過分に応じて検索窓を広げる (下限 refresh_lookback_min、上限 72h)。"""
    base = config.economic_calendar.refresh_lookback_min
    with _status_lock:
        last = _status["last_success"]
    if last is None:
        return _LOOKBACK_CAP_MIN
    elapsed_min = max(0.0, (db_now() - datetime.fromisoformat(last)).total_seconds() / 60)
    return min(_LOOKBACK_CAP_MIN, max(base, int(elapsed_min) + base))


def _finalize_status(*, completed: bool, failed: int, pending: int) -> None:
    """実行結果を status に反映する。last_success は completed かつ failed==0 のときのみ更新。"""
    with _status_lock:
        _status["failed"] = failed
        _status["pending"] = pending
        if completed and failed == 0:
            _status["last_success"] = db_now().isoformat()


async def collect_econ_impact(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    tradeable: list[InstrumentConfig],
) -> None:
    """経済指標影響分析 (spec §2.K)。enabled でなければ即 return。"""
    if not config.economic_calendar.enabled:
        return
    with _status_lock:
        _status["last_attempt"] = db_now().isoformat()
    failed_count = 0
    completed = False
    econ_store = None
    try:
        from src.data.econ_event_store import EconEventStore
        from src.jobs.econ_calendar_fetcher import refresh_recent_events
        from src.analysis.econ_impact_analyzer import (
            analyze_event_impact, PairReaction, SnapshotBrief
        )
        from src.analysis.economic_calendar import classify_surprise
        from src.rag.embedder import make_embed_fn

        econ_store = EconEventStore(config.econ_db_path)
        lookback = _dynamic_lookback_min(config)

        # 3a. actual更新
        refresh_recent_events(
            econ_store,
            lookback_min=lookback,
            currencies=config.economic_calendar.currencies,
        )

        # 3b. 未分析イベントを取得
        events_to_analyze = econ_store.get_unanalyzed_with_actual(
            lookback_min=lookback,
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
                            event_time_naive = to_db_naive_datetime(ev.event_time)
                            _win_start, _win_end = _econ_window_for(ev.event_time)
                            # p は tradeable 由来 → cache は _ohlcv_interval_for
                            # (= ohlcv_interval) で書かれる。interval 無指定だと
                            # day モードで "1h" バケットを読み空になる (codex Med#1 同族)
                            pd_ = price_store.load_ohlcv(
                                p.symbol, _win_start, _win_end,
                                interval=_ohlcv_interval_for(p, config),
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
                    failed_count += 1
        completed = True
    except Exception as e:
        logger.warning(f"[ECON] Economic calendar phase failed: {e}")
    finally:
        # pending は自分のカウントでなく DB 再取得 (全件失敗でも残存が見える)
        remaining = 0
        if econ_store is not None:
            try:
                remaining = len(econ_store.get_unanalyzed_with_actual(
                    lookback_min=_LOOKBACK_CAP_MIN,
                    min_importance=config.economic_calendar.post_event_impact_min,
                ))
            except Exception:
                remaining = -1
        _finalize_status(completed=completed, failed=failed_count, pending=remaining)


def run_econ_impact_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    price_provider=None,
) -> None:
    """schedule から呼ぶ同期ラッパー (LLM slot 経由で実行される)。"""
    asyncio.run(collect_econ_impact(
        config, store, price_store, analysis_store, config.tradeable_instruments,
    ))
