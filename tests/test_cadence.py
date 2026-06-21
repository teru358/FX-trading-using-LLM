"""cadence resolver / driver / econ source (Phase1 Task B, §5.3 / §5.6)。

- CadenceResolver: most-aggressive-wins, TTL 失効, watch boost 無視, base fallback。
- CadenceDriver: interval 経過で発火, skip(False) 時 backfill, watch→trade 順。
- EconCadenceSource: 窓内 boost / 窓外 base / 該当通貨のみ。
全て now 注入で決定論 (clock 非依存)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis.economic_calendar import EconEvent
from src.config.schema import AppConfig, InstrumentConfig
from src.data.econ_event_store import EconEventStore
from src.jobs.cadence_driver import CadenceDriver
from src.orchestrator.cadence_resolver import (
    SOURCE_ECON, SOURCE_STATE, CadenceResolver,
)
from src.orchestrator.cadence_sources import EconCadenceSource

NOW = datetime(2026, 6, 21, 12, 0, 0)
NOW_UTC = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _resolver():
    return CadenceResolver(
        trade_pairs=["USDJPY=X"], watch_pairs=["EURUSD=X"],
        trade_base_interval_sec=3600, watch_base_interval_sec=7200,
    )


# ── CadenceResolver ───────────────────────────────────────────

def test_base_interval_trade_vs_watch():
    r = _resolver()
    assert r.base_interval("USDJPY=X") == 3600
    assert r.base_interval("EURUSD=X") == 7200
    # boost 無しの effective は base。
    assert r.effective_interval("USDJPY=X", NOW) == 3600


def test_most_aggressive_wins():
    r = _resolver()
    r.set_boost("USDJPY=X", SOURCE_ECON, 600, NOW + timedelta(hours=1))
    r.set_boost("USDJPY=X", SOURCE_STATE, 300, NOW + timedelta(hours=1))
    # 2 経路のうち最短 (300) を採用。
    assert r.effective_interval("USDJPY=X", NOW) == 300


def test_ttl_expiry_returns_to_base():
    r = _resolver()
    r.set_boost("USDJPY=X", SOURCE_ECON, 300, NOW + timedelta(minutes=10))
    assert r.effective_interval("USDJPY=X", NOW) == 300
    # 窓を過ぎたら base に戻る (lazy expire)。
    assert r.effective_interval("USDJPY=X", NOW + timedelta(minutes=11)) == 3600


def test_watch_boost_ignored():
    r = _resolver()
    # watch pair への boost は受理されない (base 固定)。
    accepted = r.set_boost("EURUSD=X", SOURCE_ECON, 300, NOW + timedelta(hours=1))
    assert accepted is False
    assert r.effective_interval("EURUSD=X", NOW) == 7200


def test_boost_longer_than_base_ignored():
    r = _resolver()
    # base(3600) より長い boost は採用しない (boost は短縮目的)。
    r.set_boost("USDJPY=X", SOURCE_ECON, 9999, NOW + timedelta(hours=1))
    assert r.effective_interval("USDJPY=X", NOW) == 3600


def test_prune_removes_expired():
    r = _resolver()
    r.set_boost("USDJPY=X", SOURCE_ECON, 300, NOW + timedelta(minutes=5))
    assert r.prune(NOW + timedelta(minutes=6)) == 1
    assert r.active_boosts(NOW + timedelta(minutes=6)) == {}


# ── CadenceDriver ─────────────────────────────────────────────

def _driver(resolver, fired_log, *, watch_returns=None):
    def run_trade(pair):
        fired_log.append(("trade", pair))

    def run_watch(pair):
        fired_log.append(("watch", pair))
        return watch_returns

    return CadenceDriver(
        resolver=resolver, trade_pairs=["USDJPY=X"], watch_pairs=["EURUSD=X"],
        run_trade=run_trade, run_watch=run_watch,
    )


def test_driver_first_tick_fires_all():
    r = _resolver()
    log = []
    d = _driver(r, log)
    d.tick(NOW)
    # 初回は両方発火、watch が先。
    assert log == [("watch", "EURUSD=X"), ("trade", "USDJPY=X")]


def test_driver_respects_interval():
    r = _resolver()
    log = []
    d = _driver(r, log)
    d.tick(NOW)
    log.clear()
    # base interval(trade=3600s) 未満の経過では再発火しない。
    d.tick(NOW + timedelta(seconds=100))
    assert log == []
    # interval 経過後は再発火。
    d.tick(NOW + timedelta(seconds=3600))
    assert ("trade", "USDJPY=X") in log


def test_driver_skip_backfills_next_tick():
    r = _resolver()
    log = []
    # watch が False(=slot busy skip) を返すと last_run を進めない。
    d = _driver(r, log, watch_returns=False)
    d.tick(NOW)
    log.clear()
    # わずかな経過でも未実行扱いなので次 tick で再試行 (backfill)。
    d.tick(NOW + timedelta(seconds=1))
    assert ("watch", "EURUSD=X") in log


# ── EconCadenceSource ─────────────────────────────────────────

def _config():
    cfg = AppConfig()
    cfg.instruments = [
        InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
                         base_currency="USD", quote_currency="JPY", mode="trade"),
    ]
    return cfg


def _seed(store, *, event_id, currency, importance, event_time):
    store.upsert_events([EconEvent(
        event_id=event_id, title="CPI", country="US", currency=currency,
        importance=importance, event_time=event_time,
        actual=None, forecast=None, previous=None, unit="",
    )])


def test_econ_source_boosts_inside_window(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    _seed(store, event_id="e1", currency="USD", importance=1,
          event_time=NOW_UTC + timedelta(minutes=5))
    r = _resolver()
    src = EconCadenceSource(
        config=_config(), econ_store=store, resolver=r,
        boost_interval_sec=300, trade_pairs=["USDJPY=X"], now_fn=lambda: NOW_UTC,
    )
    assert src.refresh() == 1
    assert r.effective_interval("USDJPY=X", NOW) == 300


def test_econ_source_no_boost_outside_window(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    _seed(store, event_id="e1", currency="USD", importance=1,
          event_time=NOW_UTC + timedelta(hours=3))
    r = _resolver()
    src = EconCadenceSource(
        config=_config(), econ_store=store, resolver=r,
        boost_interval_sec=300, trade_pairs=["USDJPY=X"], now_fn=lambda: NOW_UTC,
    )
    assert src.refresh() == 0
    assert r.effective_interval("USDJPY=X", NOW) == 3600


def test_econ_source_only_related_currency(tmp_path):
    store = EconEventStore(tmp_path / "econ.db")
    _seed(store, event_id="e1", currency="EUR", importance=1,
          event_time=NOW_UTC + timedelta(minutes=5))
    r = _resolver()
    src = EconCadenceSource(
        config=_config(), econ_store=store, resolver=r,
        boost_interval_sec=300, trade_pairs=["USDJPY=X"], now_fn=lambda: NOW_UTC,
    )
    # EUR イベントは USDJPY を boost しない。
    assert src.refresh() == 0
