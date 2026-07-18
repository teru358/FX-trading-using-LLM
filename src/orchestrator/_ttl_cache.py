"""pair (key) 単位の TTL + single-flight + stale-if-error キャッシュ。

watch 用 news provider (dict) と material 用 news provider (NewsSentiment) が
同一の失敗セマンティクスを共有するための汎用ヘルパ (spec §3.2/§3.4)。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheResult:
    """get() の返却。status = ok|stale|unavailable。"""
    value: Any                    # 成功値 (unavailable は None)
    status: str                   # "ok" | "stale" | "unavailable"
    success_at: datetime | None   # 最後に成功した時刻 (unavailable は None)


class TtlSingleFlightCache:
    def __init__(self, *, ttl_seconds: float, negative_ttl_seconds: float,
                 clock: Callable[[], datetime]) -> None:
        self._ttl = ttl_seconds
        self._neg_ttl = negative_ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[Any, datetime]] = {}   # key -> (value, success_at)
        self._failures: dict[str, datetime] = {}            # key -> 直近失敗時刻
        self._locks: dict[str, Lock] = defaultdict(Lock)
        self._guard = Lock()

    def get(self, key: str, producer: Callable[[], Any]) -> CacheResult:
        # lock 前の速い hit チェック (clock はここで 1 回)。
        hit = self._cache.get(key)
        if hit is not None and (self._clock() - hit[1]).total_seconds() < self._ttl:
            return CacheResult(hit[0], "ok", hit[1])
        with self._guard:
            lock = self._locks[key]
        with lock:                                 # single-flight (producer は lock 外で待たない)
            # lock 取得後に clock を再取得して再判定 (TTL 起点は判定時刻)。
            now = self._clock()
            hit = self._cache.get(key)
            if hit is not None and (now - hit[1]).total_seconds() < self._ttl:
                return CacheResult(hit[0], "ok", hit[1])
            failed_at = self._failures.get(key)
            if failed_at is not None and (now - failed_at).total_seconds() < self._neg_ttl:
                # failure TTL 内: producer を呼ばない (計算もログも止める)。
                return (CacheResult(hit[0], "stale", hit[1]) if hit is not None
                        else CacheResult(None, "unavailable", None))
            try:
                value = producer()
            except Exception as exc:
                # 失敗時刻は producer *完了後* に取得 (producer 実行時間分の起点ズレを避ける)。
                self._failures[key] = self._clock()
                logger.warning("[ORCH] cache producer failed for %s: %s", key, exc)
                return (CacheResult(hit[0], "stale", hit[1]) if hit is not None
                        else CacheResult(None, "unavailable", None))
            # 成功時刻も producer 完了後に取得 (成功 TTL が処理時間分短くならないように)。
            success_at = self._clock()
            self._cache[key] = (value, success_at)
            self._failures.pop(key, None)          # 成功で failure 解除
            return CacheResult(value, "ok", success_at)
