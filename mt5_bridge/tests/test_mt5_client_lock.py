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
