from datetime import datetime, timedelta

from src.orchestrator.context_builder import make_cached_news_provider


class _Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += timedelta(seconds=s)


def _sentiment(score):
    return {"sentiment_score": score, "confidence": 0.8, "top_reasons": ["x"]}


def test_ttl_hit_calls_inner_once():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def inner(pair):
        calls["n"] += 1
        return _sentiment(0.5)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    r1 = p("EURUSD=X")
    clock.advance(30)
    r2 = p("EURUSD=X")
    assert calls["n"] == 1
    assert r1["status"] == "ok"
    assert r1["as_of"] is not None
    assert r2["sentiment_score"] == 0.5


def test_stale_if_error_keeps_last_success():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def inner(pair):
        if state["fail"]:
            raise RuntimeError("boom")
        return _sentiment(-0.6)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    ok = p("EURUSD=X")
    success_as_of = ok["as_of"]
    clock.advance(61)
    state["fail"] = True
    stale = p("EURUSD=X")
    assert stale["status"] == "stale"
    assert stale["sentiment_score"] == -0.6
    assert stale["as_of"] == success_as_of


def test_unavailable_when_never_succeeded():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def inner(pair):
        raise RuntimeError("boom")
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    r = p("EURUSD=X")
    assert r["status"] == "unavailable"
    assert r["sentiment_score"] is None
    assert r["as_of"] is None
