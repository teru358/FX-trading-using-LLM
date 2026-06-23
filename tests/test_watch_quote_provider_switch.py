from datetime import datetime

from src.data.quote_stream import QuoteStreamProducer
from src.orchestrator.context_builder import QuoteSnapshot


class _StubProducer:
    def __init__(self, snap):
        self._snap = snap
        self.start_called = False
        self.stop_called = False

    def latest(self, pair):
        return self._snap

    def start(self):
        self.start_called = True

    def stop(self):
        self.stop_called = True


def _snap():
    return QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )


def test_make_producer_quote_provider_reads_latest():
    from src.orchestrator.bootstrap import make_producer_quote_provider

    prod = _StubProducer(_snap())
    fallback_called = []

    def fallback(pair):
        fallback_called.append(pair)
        return _snap()

    provider = make_producer_quote_provider(prod, fallback)
    snap = provider("USDJPY=X")
    assert snap.source == "mt5"
    assert fallback_called == []  # latest が非 None なら fallback を呼ばない


def test_make_producer_quote_provider_falls_back_when_latest_none():
    from src.orchestrator.bootstrap import make_producer_quote_provider

    prod = _StubProducer(None)  # まだ poll 未完了
    fallback_called = []

    def fallback(pair):
        fallback_called.append(pair)
        return _snap()

    provider = make_producer_quote_provider(prod, fallback)
    snap = provider("USDJPY=X")
    assert snap is not None
    assert fallback_called == ["USDJPY=X"]  # None のとき従来 fetch にフォールバック
