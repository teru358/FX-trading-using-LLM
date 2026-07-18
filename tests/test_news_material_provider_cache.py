"""material news provider の TTL 共有キャッシュ + 例外伝搬 (B-2)。

get_news_impact / get_news_key が同一 pair の連続呼び出しで aggregate を 1 回に
集約すること、成功後の refresh 失敗で stale-if-error が効くこと、恒常失敗で
unavailable (impact=0.0 / key=None) になり negative TTL 内は再計算しないことを検証。
"""
from datetime import datetime, timedelta

import pytest

from src.config.schema import AppConfig, InstrumentConfig
from src.orchestrator.landing_providers import make_news_material_provider


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


class _Sent:
    def __init__(self, score, summary):
        self.sentiment_score = score
        self.summary = summary


def _config():
    return AppConfig(instruments=[
        InstrumentConfig(symbol="EURUSD=X", display_name="EUR/USD",
                         base_currency="EUR", quote_currency="USD", pip_value=0.0001),
    ])


def test_impact_and_key_aggregate_once(monkeypatch):
    """impact + key 連続呼び出しで aggregate は 1 回に集約 (TTL 内)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}

    def fake_aggregate(inst, store, config):
        calls["n"] += 1
        return _Sent(0.6, "sum")

    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    impact("EURUSD=X")
    key("EURUSD=X")
    assert calls["n"] == 1


def test_material_stale_if_error(monkeypatch):
    """成功後の refresh 失敗で前回 sentiment (impact) が維持される。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}

    def fake_aggregate(inst, store, config):
        if state["fail"]:
            raise RuntimeError("boom")
        return _Sent(0.6, "sum")

    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    assert impact("EURUSD=X") == pytest.approx(0.6)
    clock.advance(61)
    state["fail"] = True
    assert impact("EURUSD=X") == pytest.approx(0.6)      # stale 維持


def test_material_unavailable_returns_zero(monkeypatch):
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))

    def fake_aggregate(inst, store, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    assert impact("EURUSD=X") == 0.0
    assert key("EURUSD=X") is None


def test_material_failure_ttl_suppresses_recompute(monkeypatch):
    """失敗後 negative TTL 内は aggregate を再度呼ばない (計算・ログ抑止)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}

    def fake_aggregate(inst, store, config):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    impact("EURUSD=X")                    # 1 回目失敗 (calls=1)
    clock.advance(10)
    key("EURUSD=X")                       # negative TTL 内 → aggregate 呼ばない
    assert calls["n"] == 1
