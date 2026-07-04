"""Mt5Client の threading.Lock による MT5 アクセス直列化テスト。"""
from __future__ import annotations

import threading
import time

from mt5_client import Mt5Client


class _SlowFakeMt5:
    """各 MT5 呼び出しに sleep を入れ、interleave を観測可能にする Fake。"""
    def __init__(self, call_log, lock_for_log):
        self._call_log = call_log
        self._log_lock = lock_for_log
        self.TIMEFRAME_H1 = 16385

    def _record(self, name):
        with self._log_lock:
            self._call_log.append(name)

    def symbol_select(self, symbol, enable):
        self._record(f"select:{symbol}")
        time.sleep(0.02)
        return True

    def copy_rates_range(self, symbol, tf, date_from, date_to):
        self._record(f"copy:{symbol}")
        time.sleep(0.02)
        return [{"time": 1700000000, "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0, "tick_volume": 1}]

    def last_error(self):
        return (0, "ok")


def test_copy_rates_range_is_serialized_across_threads():
    """2スレッドが copy_rates_range を同時に呼んでも select→copy ペアが
    他スレッドの呼び出しで割り込まれない (lock で直列化される)。"""
    from datetime import datetime, timezone

    call_log: list[str] = []
    client = Mt5Client.__new__(Mt5Client)
    client._mt5 = _SlowFakeMt5(call_log, threading.Lock())
    client._lock = threading.Lock()

    d0 = datetime(2026, 6, 30, tzinfo=timezone.utc)
    d1 = datetime(2026, 6, 30, 1, tzinfo=timezone.utc)

    def worker(sym):
        client.copy_rates_range(sym, "1h", d0, d1)

    t1 = threading.Thread(target=worker, args=("USDJPY",))
    t2 = threading.Thread(target=worker, args=("EURUSD",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(call_log) == 4
    for i in (0, 2):
        sym = call_log[i].split(":")[1]
        assert call_log[i] == f"select:{sym}"
        assert call_log[i + 1] == f"copy:{sym}", (
            f"interleave detected: {call_log}"
        )


def test_disconnect_does_not_interleave_with_copy_rates():
    """copy_rates_range 実行中に disconnect() (shutdown) が割り込まない
    (lifecycle も lock 対象)。"""
    from datetime import datetime, timezone

    log: list[str] = []
    log_lock = threading.Lock()

    class _Fake:
        TIMEFRAME_H1 = 16385
        def _rec(self, n):
            with log_lock:
                log.append(n)
        def symbol_select(self, s, e):
            self._rec("select"); time.sleep(0.02); return True
        def copy_rates_range(self, s, tf, a, b):
            self._rec("copy_start"); time.sleep(0.04); self._rec("copy_end")
            return [{"time": 1700000000, "open": 1.0, "high": 1.0,
                     "low": 1.0, "close": 1.0, "tick_volume": 1}]
        def shutdown(self):
            self._rec("shutdown")
        def last_error(self):
            return (0, "ok")

    c = Mt5Client.__new__(Mt5Client)
    c._mt5 = _Fake()
    c._lock = threading.Lock()
    c._connected = True

    d0 = datetime(2026, 6, 30, tzinfo=timezone.utc)
    d1 = datetime(2026, 6, 30, 1, tzinfo=timezone.utc)

    def data_worker():
        c.copy_rates_range("USDJPY", "1h", d0, d1)

    t = threading.Thread(target=data_worker)
    t.start()
    time.sleep(0.01)
    c.disconnect()
    t.join()

    assert "shutdown" in log
    ci_start, ci_end = log.index("copy_start"), log.index("copy_end")
    sd = log.index("shutdown")
    assert not (ci_start < sd < ci_end), f"shutdown interleaved: {log}"


import pytest
from mt5_client import Mt5Client, PreflightError


class _PreflightFakeMt5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def __init__(self, *, tick_ok=True, free_margin=10_000.0, select_ok=True):
        self._tick_ok = tick_ok
        self._free_margin = free_margin
        self._select_ok = select_ok

    def symbol_select(self, symbol, enable):
        return self._select_ok

    def symbol_info_tick(self, symbol):
        if not self._tick_ok:
            return None
        return type("T", (), {"ask": 150.0, "bid": 149.98})()

    def order_calc_margin(self, order_type, symbol, volume, price):
        return 100.0

    def account_info(self):
        # 実 MT5 の account_info() は margin_free を返す (get_account が free_margin に map)
        return type("A", (), {
            "login": 1, "server": "s", "currency": "JPY",
            "balance": self._free_margin, "equity": self._free_margin,
            "margin": 0.0, "margin_free": self._free_margin,
            "leverage": 100, "name": "n", "trade_mode": 0,
        })()

    def last_error(self):
        return (0, "ok")


def _preflight_client(fake):
    c = Mt5Client.__new__(Mt5Client)
    c._mt5 = fake
    c._lock = threading.Lock()
    return c


def test_preflight_order_margin_passes_when_margin_sufficient():
    c = _preflight_client(_PreflightFakeMt5(free_margin=10_000.0))
    c.preflight_order_margin("USDJPY", "buy", 0.1)  # 例外なし = 成功


def test_preflight_order_margin_raises_on_insufficient_margin():
    c = _preflight_client(_PreflightFakeMt5(free_margin=1.0))
    with pytest.raises(PreflightError) as ei:
        c.preflight_order_margin("USDJPY", "buy", 0.1)
    assert "insufficient margin" in str(ei.value).lower()


def test_preflight_order_margin_raises_when_symbol_unavailable():
    c = _preflight_client(_PreflightFakeMt5(select_ok=False))
    with pytest.raises(PreflightError):
        c.preflight_order_margin("USDJPY", "buy", 0.1)


def test_preflight_order_margin_raises_when_no_tick():
    c = _preflight_client(_PreflightFakeMt5(tick_ok=False))
    with pytest.raises(PreflightError):
        c.preflight_order_margin("USDJPY", "buy", 0.1)


def test_preflight_does_not_deadlock_calling_locked_helpers():
    """preflight は lock 保持のまま margin/account を計算しても self-deadlock しない
    (= *_locked private を呼んでいる)。正常に完了すれば OK。"""
    c = _preflight_client(_PreflightFakeMt5(free_margin=10_000.0))
    c.preflight_order_margin("USDJPY", "buy", 0.1)
    # 追加で public get_account / calc_required_margin も従来どおり動く (回帰)
    assert c.get_account().free_margin == 10_000.0
    assert c.calc_required_margin("USDJPY", "buy", 0.1, 150.0) == 100.0
