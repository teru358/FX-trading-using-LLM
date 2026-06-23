"""quote-stream producer (spec §4)。trade pairs を短周期 polling し最新 quote を保持する。

MT5 enabled なら bridge /quote (bid/ask 実値、spread=ask-bid) を引く。失敗 / MT5 非対象は
/ohlcv 等の get_current_price (mid only, spread=None) へ degrade。取得例外時は最新値を
更新せず、古い observed_at を残す (watch の freshness wall が stale を検知して止める)。
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError
from src.orchestrator.context_builder import QuoteSnapshot
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher
    from src.data.price_provider import PriceProvider

logger = logging.getLogger(__name__)


class QuoteStreamProducer:
    def __init__(
        self, *, pairs: list[str], fetcher: "Mt5OhlcvFetcher | None",
        price_provider: "PriceProvider", mt5_enabled: bool, poll_seconds: int,
    ) -> None:
        self._pairs = list(pairs)
        self._fetcher = fetcher
        self._price_provider = price_provider
        self._mt5_enabled = mt5_enabled
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._latest: dict[str, QuoteSnapshot] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _build_snapshot(self, pair: str) -> QuoteSnapshot:
        """1 pair の最新 QuoteSnapshot を作る。例外は呼び出し側へ伝播。"""
        if self._mt5_enabled and self._fetcher is not None:
            try:
                q = self._fetcher.get_quote(pair)
                return QuoteSnapshot(
                    bid=q.bid, ask=q.ask, mid=q.mid, spread=q.spread,
                    source=q.source, observed_at=q.observed_at,
                )
            except Mt5UnreachableError:
                pass  # degrade to /ohlcv
        cp = self._price_provider.get_current_price(pair)
        observed = cp.timestamp or db_now()
        return QuoteSnapshot(
            bid=cp.price, ask=cp.price, mid=cp.price, spread=None,
            source=cp.source, observed_at=observed,
        )

    def poll_once(self) -> None:
        """全 pair を 1 回 poll する。pair 単位の取得失敗は最新値を更新しないだけ。"""
        for pair in self._pairs:
            try:
                snap = self._build_snapshot(pair)
            except Exception:
                logger.exception("[QUOTE-STREAM] build snapshot failed for %s", pair)
                continue  # 最新値を更新しない (古い observed_at が残る)
            with self._lock:
                self._latest[pair] = snap

    def latest(self, pair: str) -> "QuoteSnapshot | None":
        with self._lock:
            return self._latest.get(pair)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(timeout=self._poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="quote-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
