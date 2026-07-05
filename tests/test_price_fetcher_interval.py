"""fetch_ohlcv (yfinance 経路) の interval 伝搬 (codex Med#1)。

fallback fetcher (yfinance) 側でも PriceStore への呼び出しに interval が伝わらないと、
day モード (ohlcv_interval=15m) でキャッシュが "1h" バケットへ誤って書き込まれ/読み出され、
差分フェッチの刻みも 1h 固定になり 15m で 45 分取り逃がす (mt5 経路は既に対応済み、
tests/test_mt5_fetch_interval_step.py 参照)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.data import price_fetcher


def _ohlcv_df(n=30, freq="15min"):
    idx = pd.date_range("2026-07-01", periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
         "Close": [1.0] * n, "Volume": [0.0] * n},
        index=idx,
    )


class _FakeStore:
    """interval 伝搬を記録する fake PriceStore。"""

    def __init__(self, latest):
        self._latest = latest
        self.calls = []

    def get_latest_date(self, symbol, *, interval="1h"):
        self.calls.append(("latest", interval))
        return self._latest

    def get_earliest_date(self, symbol, *, interval="1h"):
        # hist_start より古い earliest を返し、過去方向補完 (Step1) をスキップさせる
        self.calls.append(("earliest", interval))
        return datetime(2020, 1, 1)

    def load_ohlcv(self, symbol, start, end, *, interval="1h"):
        self.calls.append(("load", interval))
        return _ohlcv_df()  # >= 20 本で DB 経路から return させる

    def upsert_ohlcv(self, symbol, df, *, interval="1h"):
        self.calls.append(("upsert", interval))


def test_diff_fetch_starts_at_latest_plus_interval_and_propagates(monkeypatch):
    """interval='15m' のとき差分起点 = latest + 15min、store 呼び出しは全て interval='15m'。"""
    latest = datetime(2026, 7, 1, 10, 0)
    store = _FakeStore(latest)

    captured = {}

    def _fake_yf_fetch_range(symbol, start, interval):
        captured["fetch_from"] = start
        captured["interval"] = interval
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    monkeypatch.setattr(price_fetcher, "_yf_fetch_range", _fake_yf_fetch_range)

    price_fetcher.fetch_ohlcv("USDJPY=X", period="7d", interval="15m", price_store=store)

    assert captured["fetch_from"] == latest + timedelta(minutes=15)
    assert captured["interval"] == "15m"
    # store の全 API に interval="15m" が伝搬していること
    assert ("latest", "15m") in store.calls
    assert ("load", "15m") in store.calls
    assert all(call[1] == "15m" for call in store.calls)
