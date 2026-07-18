"""news / event landing provider (Phase1 Task A-2, §5.4①)。

make_event_window_provider が高重要度イベントの [event−10m, event+30m] 窓を判定し
event_id を key にすること、該当通貨のみ反応すること、窓外で False になることを検証する。
make_news_material_provider と MaterialLandingDetector への配線も確認する。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis.economic_calendar import EconEvent
from src.config.schema import AppConfig, InstrumentConfig
from src.data.econ_event_store import EconEventStore
from src.orchestrator.landing_providers import (
    make_event_window_provider,
    make_news_material_provider,
)
from src.orchestrator.material_landing import MaterialLandingDetector

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock():
    return datetime(2026, 7, 17, 0, 0, 0)


def _config_with_usdjpy() -> AppConfig:
    cfg = AppConfig()
    cfg.instruments = [
        InstrumentConfig(
            symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
            base_currency="USD", quote_currency="JPY", mode="trade",
        )
    ]
    return cfg


def _seed_event(store: EconEventStore, *, event_id, currency, importance, event_time):
    # EconEvent.importance: -1=low / 0=medium / 1=high。
    store.upsert_events([
        EconEvent(
            event_id=event_id, title="CPI", country="US", currency=currency,
            importance=importance, event_time=event_time,
            actual=None, forecast=None, previous=None, unit="",
        )
    ])


def test_in_event_window_true_inside_window(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    # USD イベントを now の 5 分後に置く (窓 [event−10, event+30] に now が入る)。
    _seed_event(store, event_id="e1", currency="USD", importance=1,
                event_time=NOW + timedelta(minutes=5))
    cfg = _config_with_usdjpy()
    in_window, get_key = make_event_window_provider(
        cfg, store, now_fn=lambda: NOW,
    )
    assert in_window("USDJPY=X") is True
    assert get_key("USDJPY=X") == "e1"


def test_outside_window_false(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    # event が now の 2 時間後 = 窓外。
    _seed_event(store, event_id="e1", currency="USD", importance=1,
                event_time=NOW + timedelta(hours=2))
    cfg = _config_with_usdjpy()
    in_window, get_key = make_event_window_provider(cfg, store, now_fn=lambda: NOW)
    assert in_window("USDJPY=X") is False
    assert get_key("USDJPY=X") is None


def test_unrelated_currency_ignored(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    # EUR イベントは USDJPY に無関係。
    _seed_event(store, event_id="e1", currency="EUR", importance=1,
                event_time=NOW + timedelta(minutes=5))
    cfg = _config_with_usdjpy()
    in_window, _ = make_event_window_provider(cfg, store, now_fn=lambda: NOW)
    assert in_window("USDJPY=X") is False


def test_low_importance_ignored(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    # medium 重要度 (0) のイベントは high (min_importance=1) フィルタで無視される。
    _seed_event(store, event_id="e1", currency="USD", importance=0,
                event_time=NOW + timedelta(minutes=5))
    cfg = _config_with_usdjpy()
    in_window, _ = make_event_window_provider(
        cfg, store, min_importance=1, now_fn=lambda: NOW,
    )
    assert in_window("USDJPY=X") is False


def test_event_landing_drives_detector_then_consumed(tmp_path):
    """event 窓内で material になり、同一 key を消費後は再発火しない。"""
    store = EconEventStore(tmp_path / "econ.db")
    _seed_event(store, event_id="e1", currency="USD", importance=1,
                event_time=NOW + timedelta(minutes=5))
    cfg = _config_with_usdjpy()
    in_window, get_key = make_event_window_provider(cfg, store, now_fn=lambda: NOW)
    det = MaterialLandingDetector(
        get_latest_technical=lambda p: None,  # technical 無し
        material_bias_delta_min=0.2,
        in_event_window=in_window,
        get_event_key=get_key,
        pairs=["USDJPY=X"],
    )
    # event 窓内 → material。
    assert det.event_window_material("USDJPY=X") is True
    # 同 event を消費すると同一 key で再発火しない。
    det.commit_seen("USDJPY=X")
    assert det.event_window_material("USDJPY=X") is False


def test_news_provider_impact_and_key(tmp_path, monkeypatch):
    cfg = _config_with_usdjpy()

    class _Sent:
        sentiment_score = -0.7
        summary = "USD weak"

    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment",
        lambda inst, store, config: _Sent(),
    )
    get_impact, get_key = make_news_material_provider(
        cfg, store=object(),
        ttl_seconds=60.0, negative_ttl_seconds=30.0, clock=_fixed_clock,
    )
    assert get_impact("USDJPY=X") == 0.7  # |−0.7|
    assert get_key("USDJPY=X") is not None
    # 未知 pair は impact 0。
    assert get_impact("EURUSD=X") == 0.0
