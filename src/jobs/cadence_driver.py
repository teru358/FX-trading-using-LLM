"""可変 interval self-scheduling ドライバ (Phase1 Task B-2, §5.6)。

`schedule` ライブラリの固定 interval ではなく、薄い「毎 tick ドライバ」を 1 本だけ回し、
毎 tick **pair ごとに CadenceResolver を引いて「前回実行からの経過 ≥ 有効 interval」なら
収集を発火**する self-scheduling 方式。boost 窓は resolver が most-aggressive interval を
返すことで自然に反映され、TTL 失効で base に戻る (§5.3 / §5.6)。

**skip/backfill (§5.1.1):** 収集発火が slot busy で skip されても last_run を更新しないため、
次 tick で経過判定が再び真になり自然に backfill される (次の定期周期まで待たない)。
収集 callback が False を返したら「未実行 (skip)」とみなす。

**watch→trade 順序:** 同 tick で両方 due なら watch を先に発火する (相関入力の watch 価格を
先に更新する既存 union dispatch の思想を踏襲、§5.1)。

非LLM。`now` は呼び出し側 (driver loop) が注入する。実際の収集 callback (run_watch /
run_trade) は外部から渡す (既存 run_watch/trade_technical_collection を束ねる)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from src.orchestrator.cadence_resolver import CadenceResolver
from src.utils.clock import db_now

logger = logging.getLogger(__name__)

# 収集 callback。実行されたら True/None、slot busy で未実行なら False を返す規約。
RunCallback = Callable[[str], bool | None]


class CadenceDriver:
    """resolver を引いて due な pair の収集を発火する self-scheduling ドライバ。"""

    def __init__(
        self,
        *,
        resolver: CadenceResolver,
        trade_pairs: list[str],
        watch_pairs: list[str],
        run_trade: RunCallback,
        run_watch: RunCallback,
    ) -> None:
        self._resolver = resolver
        self._trade = list(trade_pairs)
        self._watch = list(watch_pairs)
        self._run_trade = run_trade
        self._run_watch = run_watch
        self._last_run: dict[str, datetime] = {}

    def _is_due(self, pair: str, now: datetime) -> bool:
        last = self._last_run.get(pair)
        if last is None:
            return True  # 初回は即 due (bootstrap)
        interval = self._resolver.effective_interval(pair, now)
        return (now - last).total_seconds() >= interval

    def tick(self, now: datetime | None = None) -> list[str]:
        """1 tick 分。due な pair の収集を watch→trade 順で発火する。

        Returns: 発火を試みた pair のリスト (skip 含む)。
        """
        now = now or db_now()
        fired: list[str] = []
        # watch を先に (相関入力の鮮度を trade より先に整える)。
        for pair in self._watch:
            if self._is_due(pair, now):
                if self._dispatch(self._run_watch, pair):
                    self._last_run[pair] = now
                fired.append(pair)
        for pair in self._trade:
            if self._is_due(pair, now):
                if self._dispatch(self._run_trade, pair):
                    self._last_run[pair] = now
                fired.append(pair)
        return fired

    @staticmethod
    def _dispatch(cb: RunCallback, pair: str) -> bool:
        """収集 callback を呼ぶ。実行されたとみなせるなら True。

        callback が明示的に False を返したら「slot busy で未実行 = skip」とみなし
        last_run を進めない (次 tick で backfill, §5.1.1)。None / 例外は「実行された」
        扱い (last_run を進める) — 収集側の内部失敗で毎 tick 走らせない。
        """
        try:
            result = cb(pair)
        except Exception:
            logger.exception(f"[CADENCE] collection failed for {pair}")
            return True  # 実行はした (内部失敗)。毎 tick リトライさせない。
        return result is not False
