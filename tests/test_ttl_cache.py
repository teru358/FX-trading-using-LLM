import threading
import time
from datetime import datetime, timedelta

from src.orchestrator._ttl_cache import TtlSingleFlightCache


class _Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += timedelta(seconds=s)


def _cache(clock, ttl=60, neg=30):
    return TtlSingleFlightCache(ttl_seconds=ttl, negative_ttl_seconds=neg, clock=clock)


def test_ttl_hit_calls_producer_once():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        return "v"
    c = _cache(clock)
    assert c.get("k", produce).value == "v"
    clock.advance(30)
    c.get("k", produce)
    assert calls["n"] == 1


def test_ttl_expiry_recomputes():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        return "v"
    c = _cache(clock)
    c.get("k", produce)
    clock.advance(61)
    c.get("k", produce)
    assert calls["n"] == 2


def test_failure_ttl_suppresses_recompute():
    """失敗後 negative TTL 内は producer を呼ばない (計算とログ両方を止める)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        raise RuntimeError("boom")
    c = _cache(clock)
    c.get("k", produce)
    clock.advance(10)
    c.get("k", produce)
    assert calls["n"] == 1


def test_stale_if_error_keeps_last_success():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def produce():
        if state["fail"]:
            raise RuntimeError("boom")
        return "good"
    c = _cache(clock)
    ok = c.get("k", produce)
    assert ok.status == "ok"
    clock.advance(61)
    state["fail"] = True
    stale = c.get("k", produce)
    assert stale.status == "stale"
    assert stale.value == "good"
    assert stale.success_at == ok.success_at   # 成功時刻を維持


def test_unavailable_when_never_succeeded():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def produce():
        raise RuntimeError("boom")
    c = _cache(clock)
    r = c.get("k", produce)
    assert r.status == "unavailable"
    assert r.value is None
    assert r.success_at is None


def test_success_clears_failure_state():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    state = {"fail": True}
    def produce():
        calls["n"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return "v"
    c = _cache(clock)
    c.get("k", produce)           # 失敗 (n=1)
    clock.advance(31)
    state["fail"] = False
    c.get("k", produce)           # 成功 (n=2, failure クリア)
    clock.advance(61)
    state["fail"] = True
    c.get("k", produce)           # 再失敗 → producer 呼ぶ (n=3)
    assert calls["n"] == 3


def test_failure_time_taken_after_producer_completes():
    """producer が時間を消費して失敗した場合、failure 起点は producer 完了後の時刻。

    producer 内で clock を 40s 進めて失敗 → negative TTL 30s。
    完了後 (=起点) から 10s 進めても再実行されない (起点が producer 前だと再実行されてしまう)。
    """
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        clock.advance(40)          # producer が 40s 消費
        raise RuntimeError("boom")
    c = _cache(clock, ttl=60, neg=30)
    c.get("k", produce)            # 失敗・起点は完了後 (t=40)
    clock.advance(10)              # t=50 (完了後 10s < neg 30) → 再実行しない
    c.get("k", produce)
    assert calls["n"] == 1


def test_success_time_taken_after_producer_completes():
    """producer が時間を消費して成功した場合、success 起点は producer 完了後の時刻。

    producer 内で 50s 進めて成功 → 成功 TTL 60s。完了後 (=起点) から 30s 進めても
    hit 継続 (起点が producer 前だと 60s 経過扱いで miss してしまう)。
    """
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        clock.advance(50)          # producer が 50s 消費
        return "v"
    c = _cache(clock, ttl=60, neg=30)
    c.get("k", produce)            # 成功・起点は完了後 (t=50)
    clock.advance(30)              # t=80 (完了後 30s < ttl 60) → hit 継続
    c.get("k", produce)
    assert calls["n"] == 1


def test_single_flight_concurrent_miss():
    """2 スレッド同時 miss でも producer は 1 回だけ。デッドロックしない設計。

    Barrier は producer の *外* (get 呼び出し直前) で同期する。producer 内では
    Event で 1 本目を停止し、2 本目が lock 待ちに入る猶予を与えてから release する。
    """
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    release = threading.Event()
    entered = threading.Event()
    def produce():
        calls["n"] += 1
        entered.set()             # 1 本目が producer に入った
        release.wait(timeout=2.0) # release されるまで lock を保持
        return "v"
    c = _cache(clock)
    results = {}
    def worker(name):
        results[name] = c.get("k", produce).value
    t1 = threading.Thread(target=worker, args=("a",))
    t1.start()
    assert entered.wait(timeout=2.0)   # 1 本目が producer 到達を保証 (未到達なら失敗)
    t2 = threading.Thread(target=worker, args=("b",))
    t2.start()                    # 2 本目は lock 待ちで停止 (producer に入らない)
    time.sleep(0.1)               # 2 本目が lock 待ちに入る猶予
    release.set()                 # 1 本目を進ませる
    t1.join(timeout=2.0); t2.join(timeout=2.0)
    assert not t1.is_alive() and not t2.is_alive()   # デッドロックしていない
    assert calls["n"] == 1                            # producer は 1 回だけ
    assert results == {"a": "v", "b": "v"}
