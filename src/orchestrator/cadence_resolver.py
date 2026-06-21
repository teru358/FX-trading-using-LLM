"""CadenceResolver — 収集頻度の 3 経路 boost 解決 (Phase1 Task B-1, §5.3)。

3 つの独立経路 (① 経済カレンダー / ② 市場 state / ③ Planner ヒント) がそれぞれ TTL 付き
boost を書き込み、resolver は未 expire の boost のうち **最短 interval (most-aggressive-wins)**
を採用する。boost が無ければ horizon 既定の base interval に戻る。

非LLM の純ロジック。`now` は呼び出し側が注入する (テスト容易・clock 非依存)。

**boost は trade instrument のみ対象 (§5.3 / §4.8)。** watch instrument は base interval
固定で、boost 要求は無視する (負荷を trade に集中)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# boost 経路の識別子。
SOURCE_ECON = "econ"
SOURCE_STATE = "state"
SOURCE_PLANNER = "planner"
_VALID_SOURCES = frozenset({SOURCE_ECON, SOURCE_STATE, SOURCE_PLANNER})


@dataclass
class _Boost:
    interval_sec: int
    expires_at: datetime


class CadenceResolver:
    """pair ごとの有効収集 interval を 3 経路 boost から解決する。"""

    def __init__(
        self,
        *,
        trade_pairs: list[str],
        watch_pairs: list[str],
        trade_base_interval_sec: int,
        watch_base_interval_sec: int,
    ) -> None:
        self._trade = set(trade_pairs)
        self._watch = set(watch_pairs)
        self._trade_base = max(1, trade_base_interval_sec)
        self._watch_base = max(1, watch_base_interval_sec)
        # (pair, source) -> _Boost。同一 (pair, source) は上書き。
        self._boosts: dict[tuple[str, str], _Boost] = {}

    # ── base ────────────────────────────────────────────────────

    def base_interval(self, pair: str) -> int:
        """boost 抜きの既定 interval。trade は trade_base、それ以外は watch_base。"""
        return self._trade_base if pair in self._trade else self._watch_base

    # ── boost 書き込み ──────────────────────────────────────────

    def set_boost(
        self, pair: str, source: str, interval_sec: int, expires_at: datetime
    ) -> bool:
        """boost を書く。trade pair のみ受理する。受理したら True。

        watch pair / 未知 pair / 不正 source / 非正 interval は無視 (False)。
        """
        if source not in _VALID_SOURCES:
            return False
        if pair not in self._trade:
            return False  # watch は base 固定 (§5.3)
        if interval_sec <= 0:
            return False
        self._boosts[(pair, source)] = _Boost(
            interval_sec=int(interval_sec), expires_at=expires_at
        )
        return True

    def clear_boost(self, pair: str, source: str) -> None:
        """特定経路の boost を明示的に取り消す (calm 復帰など)。"""
        self._boosts.pop((pair, source), None)

    # ── 解決 ────────────────────────────────────────────────────

    def effective_interval(self, pair: str, now: datetime) -> int:
        """現在の有効 interval を返す (most-aggressive-wins)。

        未 expire の boost のうち最短 interval を採用。全 expire / boost 無しなら base。
        base より長い boost は採用しない (boost は短くするためのもの)。
        """
        base = self.base_interval(pair)
        best = base
        for (p, _src), boost in self._boosts.items():
            if p != pair:
                continue
            if boost.expires_at <= now:
                continue  # lazy expire
            if boost.interval_sec < best:
                best = boost.interval_sec
        return best

    def prune(self, now: datetime) -> int:
        """expire 済み boost を物理削除する。削除件数を返す (掃除用・任意)。"""
        dead = [k for k, b in self._boosts.items() if b.expires_at <= now]
        for k in dead:
            del self._boosts[k]
        return len(dead)

    def active_boosts(self, now: datetime) -> dict[tuple[str, str], int]:
        """未 expire の boost を {(pair, source): interval_sec} で返す (観測用)。"""
        return {
            k: b.interval_sec
            for k, b in self._boosts.items()
            if b.expires_at > now
        }
