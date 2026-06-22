from datetime import datetime

import httpx
import pytest

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, Mt5UnreachableError


class _FakeResp:
    def __init__(self, status_code: int, json_body: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=None, response=None
            )

    def json(self) -> dict:
        return self._json


def _fetcher() -> Mt5OhlcvFetcher:
    return Mt5OhlcvFetcher(
        bridge_url="http://localhost:8812", request_timeout=5.0, api_key=""
    )


def test_get_quote_returns_bid_ask_mid_spread(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        captured["url"] = url
        return _FakeResp(
            200,
            {
                "symbol": "USDJPY",
                "bid": 150.000,
                "ask": 150.020,
                "spread_points": 20,
                "time": "2026-06-22T00:00:00+00:00",  # UTC aware
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    q = _fetcher().get_quote("USDJPY=X")

    # symbol 変換: URL は MT5 形式 (=X 除去)
    assert captured["url"].endswith("/quote/USDJPY")
    # spread は価格差 (ask - bid)、pips ではない
    assert q.spread == pytest.approx(0.020)
    assert q.mid == pytest.approx(150.010)
    assert q.bid == pytest.approx(150.000)
    assert q.ask == pytest.approx(150.020)
    assert q.source == "mt5"


def test_get_quote_observed_at_is_naive_local(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        return _FakeResp(
            200,
            {
                "symbol": "USDJPY", "bid": 150.0, "ask": 150.02,
                "spread_points": 20, "time": "2026-06-22T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    q = _fetcher().get_quote("USDJPY=X")

    # observed_at は naive (tzinfo を剥がした local 値)。aware だと runtime が
    # naive now との引き算で TypeError → quote_age_sec=None → 全 block。
    assert isinstance(q.observed_at, datetime)
    assert q.observed_at.tzinfo is None


def test_get_quote_unreachable_raises(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        return _FakeResp(503)

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(Mt5UnreachableError):
        _fetcher().get_quote("USDJPY=X")


def test_get_quote_sends_api_key_header(monkeypatch):
    """api_key 付き fetcher は X-Bridge-Api-Key ヘッダを送る (review M-d)。"""
    captured = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        captured["headers"] = headers
        return _FakeResp(
            200,
            {"symbol": "USDJPY", "bid": 150.0, "ask": 150.02,
             "spread_points": 20, "time": "2026-06-22T00:00:00+00:00"},
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    f = Mt5OhlcvFetcher(
        bridge_url="http://localhost:8812", request_timeout=5.0, api_key="secret"
    )
    f.get_quote("USDJPY=X")
    assert captured["headers"] == {"X-Bridge-Api-Key": "secret"}
