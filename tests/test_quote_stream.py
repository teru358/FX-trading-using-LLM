from datetime import datetime, timedelta

import pytest

from src.data.quote_stream import QuoteStreamProducer
from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError, Quote


class _FakeFetcher:
    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes
        self.calls: list[str] = []

    def get_quote(self, pair: str) -> Quote:
        self.calls.append(pair)
        q = self._quotes.get(pair)
        if q is None:
            raise Mt5UnreachableError(f"no quote for {pair}")
        return q


class _FakePriceProvider:
    """/ohlcv fallback 用 (mid only, spread=None)。"""
    def __init__(self, price: float) -> None:
        self._price = price

    def get_current_price(self, pair: str):
        from src.data.price_fetcher import CurrentPrice
        return CurrentPrice(price=self._price, timestamp=datetime.now(), source="yfinance")


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
        spread_pips=(ask - bid) / 0.01, observed_at=datetime.now(), source="mt5",
    )


def test_poll_once_populates_latest_from_quote():
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    assert snap is not None
    # ask-bid を IEEE-754 で引くと 0.02000...0102 になるため approx で比較する
    # (plan の == 0.02 は float 精度で flaky)。狙いは「実 bid/ask 価格差が透過する」こと。
    assert snap.spread == pytest.approx(0.02)  # 価格差 (ask-bid)
    assert snap.source == "mt5"
    assert snap.observed_at.tzinfo is None


def test_quote_failure_degrades_to_ohlcv_with_spread_none():
    fetcher = _FakeFetcher({})  # 常に Mt5UnreachableError
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    assert snap is not None
    assert snap.spread is None       # degrade: spread 不明 → 安全側
    assert snap.bid == snap.ask == snap.mid == 150.0


def test_provider_exception_keeps_old_snapshot():
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    old = prod.latest("USDJPY=X")

    # 以後 get_quote も price_provider も例外 → 最新値を更新しない
    def boom(pair):
        raise RuntimeError("explode")
    fetcher.get_quote = boom
    prod._price_provider.get_current_price = boom  # type: ignore[attr-defined]
    prod.poll_once()

    assert prod.latest("USDJPY=X") is old  # 古い snapshot がそのまま残る


def test_latest_unknown_pair_is_none():
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=_FakeFetcher({}),
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True, poll_seconds=2,
    )
    assert prod.latest("EURUSD=X") is None
