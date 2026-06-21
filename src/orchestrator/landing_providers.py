"""MaterialLandingDetector の news / event 経路 provider (Phase1 Task A-2, §5.4①)。

bootstrap が detector に注入する callable 群をここに切り出す。重い import (broker /
store factory) を持つ bootstrap から分離し、純ロジックを独立テスト可能にする。

- event 経路: EconEventStore の高重要度イベント window `[event−pre, event+post]` 内かを
  判定する `in_event_window(pair)` と、その window identity `get_event_key(pair)`。
- news 経路: 既存 news 集計 (aggregate_news_sentiment) の |sentiment| を impact とみなす
  `get_news_impact(pair)` と、その news identity `get_news_key(pair)`。

pair→通貨マッピングは InstrumentConfig.related_currencies を使い、該当通貨のイベント/
news のみを material 源とする。trade instrument のみが detector の pairs に入る前提
(§4.8) なので watch 除外はここでは不要。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from src.utils.clock import db_utc_now

if TYPE_CHECKING:
    from src.config.schema import AppConfig
    from src.data.econ_event_store import EconEventStore
    from src.rag.vector_store import VectorStore


# event window の既定幅 (§5.3 経路①: [event−10min, event+30min])。
_EVENT_PRE_MIN = 10
_EVENT_POST_MIN = 30


def _to_naive_utc(dt: datetime) -> datetime:
    """tz-aware なら UTC へ寄せて naive 化、naive はそのまま (store は naive UTC 保存)。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _currencies_for(config: "AppConfig", pair: str) -> list[str]:
    """pair symbol から関連通貨リストを引く (未知 pair は空)。"""
    for inst in config.instruments:
        if inst.symbol == pair:
            return inst.related_currencies
    return []


def make_event_window_provider(
    config: "AppConfig",
    econ_store: "EconEventStore",
    *,
    min_importance: int = 1,   # EconEvent.importance: -1=low / 0=medium / 1=high
    pre_min: int = _EVENT_PRE_MIN,
    post_min: int = _EVENT_POST_MIN,
    now_fn: Callable[[], datetime] = db_utc_now,
) -> tuple[Callable[[str], bool], Callable[[str], str | None]]:
    """(in_event_window, get_event_key) を返す。

    in_event_window(pair): now がいずれかの該当通貨・高重要度イベントの
      [event−pre, event+post] 窓に入っていれば True。
    get_event_key(pair): その窓を一意化する key (event_id)。複数該当時は最も近い
      (now との差が最小の) イベントを採用。窓外なら None。
    """

    def _active_event(pair: str) -> str | None:
        currencies = _currencies_for(config, pair)
        if not currencies:
            return None
        now_naive = _to_naive_utc(now_fn())
        # now を含みうるイベント (event_time が [now−post, now+pre]) を取得する。
        events = econ_store.get_events_in_window(
            start=now_naive - timedelta(minutes=post_min),
            end=now_naive + timedelta(minutes=pre_min),
            min_importance=min_importance,
        )
        best_key: str | None = None
        best_dist: float | None = None
        for ev in events:
            if ev.currency not in currencies:
                continue
            et_naive = _to_naive_utc(ev.event_time)
            win_start = et_naive - timedelta(minutes=pre_min)
            win_end = et_naive + timedelta(minutes=post_min)
            if win_start <= now_naive <= win_end:
                dist = abs((now_naive - et_naive).total_seconds())
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_key = ev.event_id
        return best_key

    def in_event_window(pair: str) -> bool:
        return _active_event(pair) is not None

    def get_event_key(pair: str) -> str | None:
        return _active_event(pair)

    return in_event_window, get_event_key


def make_news_material_provider(
    config: "AppConfig",
    store: "VectorStore",
) -> tuple[Callable[[str], float], Callable[[str], str | None]]:
    """(get_news_impact, get_news_key) を返す。

    get_news_impact(pair): 該当 pair の直近 news sentiment の絶対値 (= impact 強度)。
    get_news_key(pair): その news 集計の identity (summary hash)。同一 news の二度発火を
      防ぐため detector が消費 key として使う。集計不能時は impact=0.0 / key=None。
    """
    from src.analysis.news_aggregator import aggregate_news_sentiment

    def _aggregate(pair: str):
        for inst in config.instruments:
            if inst.symbol == pair:
                try:
                    return aggregate_news_sentiment(inst, store, config)
                except Exception:
                    return None
        return None

    def get_news_impact(pair: str) -> float:
        sent = _aggregate(pair)
        if sent is None or sent.sentiment_score is None:
            return 0.0
        return abs(sent.sentiment_score)

    def get_news_key(pair: str) -> str | None:
        sent = _aggregate(pair)
        if sent is None:
            return None
        # summary を identity に使う (同一集計 = 同一 summary)。空なら score 由来。
        basis = sent.summary or f"score={sent.sentiment_score}"
        return str(hash(basis))

    return get_news_impact, get_news_key
