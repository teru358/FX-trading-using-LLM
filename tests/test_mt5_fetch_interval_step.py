"""差分フェッチの刻みが interval 連動であること (spec S-4a: 1h 固定だと 15m で 45 分取り逃がす)。"""
from datetime import datetime, timedelta

import pandas as pd

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, _interval_delta


def test_interval_delta_15m():
    assert _interval_delta("15m") == timedelta(minutes=15)


def test_interval_delta_1h():
    assert _interval_delta("1h") == timedelta(hours=1)


def test_interval_delta_1d():
    assert _interval_delta("1d") == timedelta(days=1)


def test_interval_delta_unknown_falls_back_to_1h():
    assert _interval_delta("bogus") == timedelta(hours=1)


def _ohlcv_df(n=30, freq="15min"):
    idx = pd.date_range("2026-07-01", periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
         "Close": [1.0] * n, "Volume": [0.0] * n},
        index=idx,
    )


class _FakeStore:
    """interval 伝搬と差分起点を記録する fake PriceStore。"""

    def __init__(self, latest):
        self._latest = latest
        self.calls = []

    def get_latest_date(self, symbol, *, interval="1h"):
        self.calls.append(("latest", interval))
        return self._latest

    def get_earliest_date(self, symbol, *, interval="1h"):
        # hist_start より古い earliest を返し、過去方向補完 (Step1) をスキップさせる
        return datetime(2020, 1, 1)

    def load_ohlcv(self, symbol, start, end, *, interval="1h"):
        self.calls.append(("load", interval))
        return _ohlcv_df()  # >= 20 本で DB 経路から return させる

    def upsert_ohlcv(self, symbol, df, *, interval="1h"):
        self.calls.append(("upsert", interval))


def test_diff_fetch_starts_at_latest_plus_interval(monkeypatch):
    """interval='15m' のとき差分起点 = latest + 15min であること (実経路)。"""
    latest = datetime(2026, 7, 1, 10, 0)
    store = _FakeStore(latest)
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)

    captured = {}

    def _fake_fetch_and_upsert(symbol, start, end, *, interval, price_store):
        captured["start"] = start
        captured["interval"] = interval

    monkeypatch.setattr(fetcher, "_fetch_and_upsert", _fake_fetch_and_upsert)

    fetcher.fetch("USDJPY=X", period="7d", interval="15m", price_store=store)

    assert captured["start"] == latest + timedelta(minutes=15)
    assert captured["interval"] == "15m"
    # store の全 API に interval="15m" が伝搬していること
    assert ("latest", "15m") in store.calls
    assert ("load", "15m") in store.calls
