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

from src.orchestrator.cadence_resolver import (
    SOURCE_ECON, SOURCE_STATE, CadenceResolver,
)
from src.orchestrator.market_state_detector import ACTIVE, CRITICAL
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


class MarketStateBridge:
    """market state を cadence boost (経路②) と regime 変化イベントに橋渡しする (Task C-2/C-3)。

    detector が出した pair 別 state を受け、(a) active/critical の trade pair に state boost
    を書く、(b) state が **上がった** (regime 変化) 際に planning 再計画コールバックを呼ぶ。
    boost の TTL は短め (state は reactive なので calm 復帰で速やかに base へ戻したい)。

    regime コールバックは material フィルタ + debounce を持つ既存機構 (MaterialLanding
    Detector 経由) に委ねる前提で、ここでは「state が上がった事実」だけを通知する。
    """

    def __init__(
        self,
        *,
        resolver: CadenceResolver | None,
        boost_interval_sec: int,
        boost_ttl_sec: int,
        on_regime_change: Callable[[str, str], None] | None = None,
    ) -> None:
        # resolver=None の場合は cadence boost を書かず regime コールバックのみ駆動する
        # (cadence_driver と runtime が別経路で resolver を共有しない構成での縮退モード)。
        self._resolver = resolver
        self._boost_interval = boost_interval_sec
        self._boost_ttl = boost_ttl_sec
        self._on_regime = on_regime_change
        self._last_state: dict[str, str] = {}
        self._rank = {"calm": 0, "normal": 1, ACTIVE: 2, CRITICAL: 3}

    def update(self, pair: str, state: str, now: datetime) -> None:
        """1 pair の state を反映する。boost 書込 + regime 上昇でコールバック。"""
        prev = self._last_state.get(pair, "normal")
        # (a) cadence boost (経路②): active/critical の間だけ boost。resolver 未接続なら skip。
        if self._resolver is not None:
            if state in (ACTIVE, CRITICAL):
                self._resolver.set_boost(
                    pair, SOURCE_STATE, self._boost_interval,
                    now + timedelta(seconds=self._boost_ttl),
                )
            else:
                # calm/normal へ戻ったら state boost を即取り消す (TTL を待たない)。
                self._resolver.clear_boost(pair, SOURCE_STATE)
        # (b) regime 変化: state が上がった (rank 増) ときだけ通知。
        if self._rank.get(state, 1) > self._rank.get(prev, 1):
            if self._on_regime is not None:
                self._on_regime(pair, state)
        self._last_state[pair] = state
