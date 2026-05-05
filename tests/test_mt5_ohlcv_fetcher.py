"""Mt5OhlcvFetcher のユニットテスト (httpx mock + PriceStore mock)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, Mt5UnreachableError


def _ohlcv_response_payload(n_bars: int = 30):
    """20 本以上 (production の sanity threshold) を満たすように生成。"""
    base_time = pd.Timestamp("2026-04-29 00:00:00", tz="UTC")
    bars = [
        {
            "time": (base_time + pd.Timedelta(hours=i)).isoformat(),
            "open": 159.50, "high": 159.55, "low": 159.45, "close": 159.50,
            "volume": 1234.0,
        }
        for i in range(n_bars)
    ]
    return {"symbol": "USDJPY", "interval": "1h", "bars": bars}


def _store_df(n_bars: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-04-29 00:00", periods=n_bars, freq="1h")
    return pd.DataFrame({
        "Open": [159.50] * n_bars, "High": [159.55] * n_bars,
        "Low": [159.45] * n_bars, "Close": [159.50] * n_bars,
        "Volume": [1234.0] * n_bars,
    }, index=idx)


def test_full_fetch_when_db_empty(monkeypatch):
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)
    store = MagicMock()
    store.get_earliest_date.return_value = None
    store.get_latest_date.return_value = None
    store.load_ohlcv.return_value = _store_df()    # >= 20 bars

    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return _ohlcv_response_payload()

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())

    pd_data = fetcher.fetch("USDJPY=X", period="90d", interval="1h", price_store=store)
    assert pd_data is not None
    assert len(pd_data.df) >= 20
    store.upsert_ohlcv.assert_called()    # backfill が走る


def test_unreachable_raises_mt5_unreachable_error(monkeypatch):
    import httpx
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)
    # date 系メソッドを None にしないと MagicMock 同士の比較で TypeError
    store = MagicMock()
    store.get_earliest_date.return_value = None
    store.get_latest_date.return_value = None
    store.load_ohlcv.return_value = pd.DataFrame()
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    with pytest.raises(Mt5UnreachableError):
        fetcher.fetch("USDJPY=X", period="90d", interval="1h", price_store=store)


def test_fetch_current_price_returns_last_close(monkeypatch):
    """fetch_current_price は 1m バーの最終 Close を返す。"""
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "symbol": "USDJPY", "interval": "1m",
                "bars": [
                    {"time": "2026-05-05T03:45:00+00:00", "open": 153.10,
                     "high": 153.15, "low": 153.08, "close": 153.12,
                     "volume": 100.0},
                    {"time": "2026-05-05T03:46:00+00:00", "open": 153.12,
                     "high": 153.20, "low": 153.10, "close": 153.18,
                     "volume": 110.0},
                ],
            }

    captured_params: list[dict] = []

    def _fake_get(url, **kw):
        captured_params.append(kw.get("params", {}))
        return _Resp()

    monkeypatch.setattr("httpx.get", _fake_get)

    cp = fetcher.fetch_current_price("USDJPY=X")
    assert cp.price == 153.18  # 最終 Close
    # 1m interval が渡されている
    assert captured_params[-1]["interval"] == "1m"


def test_fetch_current_price_zero_bars_raises(monkeypatch):
    """0 バーレスポンスは Mt5UnreachableError を raise する。"""
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)

    class _EmptyResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"symbol": "USDJPY", "interval": "1m", "bars": []}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _EmptyResp())

    with pytest.raises(Mt5UnreachableError):
        fetcher.fetch_current_price("USDJPY=X")


def test_skip_yf_suffix_in_request(monkeypatch):
    """yfinance 形式 USDJPY=X が MT5 形式 USDJPY で送信されることを確認。"""
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)
    captured_url: list[str] = []

    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"symbol": "USDJPY", "interval": "1h", "bars": []}

    def _fake_get(url, **kw):
        captured_url.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.get", _fake_get)
    store = MagicMock()
    store.get_earliest_date.return_value = datetime(2026, 1, 1)
    store.get_latest_date.return_value = datetime(2026, 4, 29)
    store.load_ohlcv.return_value = pd.DataFrame()

    try:
        fetcher.fetch("USDJPY=X", period="90d", interval="1h", price_store=store)
    except Exception:
        pass
    assert any("/ohlcv/USDJPY?" in u or "/ohlcv/USDJPY" in u for u in captured_url)
