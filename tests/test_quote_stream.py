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


def _make_producer(fetcher: _FakeFetcher) -> QuoteStreamProducer:
    return QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True, poll_seconds=2,
    )


def test_closed_market_polls_once_then_pauses(monkeypatch):
    """閉場中は最初の snapshot を張った後 poll しない (取引時間外の bridge tick 停止)。"""
    monkeypatch.setattr("src.data.quote_stream.is_market_open", lambda: False)
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = _make_producer(fetcher)

    prod._poll_if_open()
    assert len(fetcher.calls) == 1          # latest 空 → 1回だけ poll して張る
    assert prod.latest("USDJPY=X") is not None

    prod._poll_if_open()
    prod._poll_if_open()
    assert len(fetcher.calls) == 1          # 以後は閉場中 poll しない


def test_open_market_polls_every_iteration(monkeypatch):
    monkeypatch.setattr("src.data.quote_stream.is_market_open", lambda: True)
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = _make_producer(fetcher)

    prod._poll_if_open()
    prod._poll_if_open()
    assert len(fetcher.calls) == 2


def test_closed_market_keeps_polling_until_all_pairs_covered(monkeypatch):
    """閉場中でも未取得 pair が残る限り poll を続ける (部分 coverage で止めない)。"""
    monkeypatch.setattr("src.data.quote_stream.is_market_open", lambda: False)
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})  # EURUSD は quote なし
    provider = _FakePriceProvider(150.0)
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X", "EURUSD=X"], fetcher=fetcher,
        price_provider=provider, mt5_enabled=True, poll_seconds=2,
    )

    # EURUSD は degrade 先の price_provider も失敗 → snapshot が張れない
    def boom(pair):
        raise RuntimeError("explode")
    provider.get_current_price = boom  # type: ignore[method-assign]

    prod._poll_if_open()
    prod._poll_if_open()
    assert prod.latest("USDJPY=X") is not None
    assert prod.latest("EURUSD=X") is None
    assert fetcher.calls.count("EURUSD=X") == 2  # 未 coverage の間は poll 継続

    # EURUSD の quote が取れるようになったら張って停止する
    fetcher._quotes["EURUSD=X"] = _quote(1.1000, 1.1002)
    prod._poll_if_open()
    assert prod.latest("EURUSD=X") is not None
    calls_after_covered = len(fetcher.calls)
    prod._poll_if_open()
    assert len(fetcher.calls) == calls_after_covered  # 全 pair 張れたら閉場中は停止


def test_reopen_resumes_polling(monkeypatch):
    """閉場→開場の遷移で polling が再開する。"""
    open_flag = {"open": False}
    monkeypatch.setattr(
        "src.data.quote_stream.is_market_open", lambda: open_flag["open"]
    )
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = _make_producer(fetcher)

    prod._poll_if_open()                    # 閉場: 初回張りのみ
    prod._poll_if_open()
    assert len(fetcher.calls) == 1

    open_flag["open"] = True
    prod._poll_if_open()
    prod._poll_if_open()
    assert len(fetcher.calls) == 3          # 再開後は毎 iteration poll


def test_producer_snapshot_observed_at_subtractable_with_naive_now():
    """producer の observed_at が naive なので naive now と引き算でき age が出る (H1 回帰)。"""
    from src.utils.clock import db_now

    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True, poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    age = (db_now() - snap.observed_at).total_seconds()  # TypeError なら H1 退行
    assert age >= 0
