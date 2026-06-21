"""cadence boost 経路の boost 提案者 (Phase1 Task B-3, §5.3)。

CadenceResolver に TTL 付き boost を書き込む各経路の実装。本タスクでは経路① (経済
カレンダー、proactive・主経路) を実装する。経路② (市場 state) は Task C、経路③ (Planner
ヒント) は resolver.set_boost(source="planner") を直接呼べる形で API のみ用意済み。

EconCadenceSource:
  これから来る高重要度イベントの [event−pre, event+post] 窓に対し、該当 trade pair の収集
  interval を boost する (先回り予約)。boost の TTL = 窓の終端 (event+post)。窓を過ぎれば
  resolver の lazy expire で base に戻る。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from src.orchestrator.cadence_resolver import SOURCE_ECON, CadenceResolver
from src.utils.clock import db_utc_now

if TYPE_CHECKING:
    from src.config.schema import AppConfig
    from src.data.econ_event_store import EconEventStore

logger = logging.getLogger(__name__)

# event window 既定 (§5.3 経路①: [event−10min, event+30min])。
_PRE_MIN = 10
_POST_MIN = 30


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class EconCadenceSource:
    """経済カレンダー由来の cadence boost を resolver に書き込む (経路①)。"""

    def __init__(
        self,
        *,
        config: "AppConfig",
        econ_store: "EconEventStore",
        resolver: CadenceResolver,
        boost_interval_sec: int,
        trade_pairs: list[str],
        min_importance: int = 1,   # -1=low / 0=medium / 1=high
        pre_min: int = _PRE_MIN,
        post_min: int = _POST_MIN,
        now_fn: Callable[[], datetime] = db_utc_now,
    ) -> None:
        self._config = config
        self._econ = econ_store
        self._resolver = resolver
        self._boost_interval = boost_interval_sec
        self._trade = list(trade_pairs)
        self._min_importance = min_importance
        self._pre = pre_min
        self._post = post_min
        self._now_fn = now_fn
        # pair -> 関連通貨 (毎 tick の config 走査を避けてキャッシュ)。
        self._currencies: dict[str, list[str]] = {}
        for inst in config.instruments:
            if inst.symbol in self._trade:
                self._currencies[inst.symbol] = inst.related_currencies

    def refresh(self, now: datetime | None = None) -> int:
        """now 時点で窓に入っている高重要度イベントの該当 trade pair を boost する。

        boost の expires_at は窓終端 (event+post)。複数イベントが重なる場合は最も遅い
        窓終端を TTL に使う (boost を維持)。boost を書いた pair 数を返す。
        """
        now = _to_naive_utc(now or self._now_fn())
        # now を含みうるイベント (event_time が [now−post, now+pre])。
        events = self._econ.get_events_in_window(
            start=now - timedelta(minutes=self._post),
            end=now + timedelta(minutes=self._pre),
            min_importance=self._min_importance,
        )
        # pair -> 最も遅い窓終端。
        boost_until: dict[str, datetime] = {}
        for ev in events:
            et = _to_naive_utc(ev.event_time)
            win_start = et - timedelta(minutes=self._pre)
            win_end = et + timedelta(minutes=self._post)
            if not (win_start <= now <= win_end):
                continue
            for pair, currencies in self._currencies.items():
                if ev.currency in currencies:
                    cur = boost_until.get(pair)
                    if cur is None or win_end > cur:
                        boost_until[pair] = win_end
        for pair, until in boost_until.items():
            self._resolver.set_boost(pair, SOURCE_ECON, self._boost_interval, until)
        return len(boost_until)
