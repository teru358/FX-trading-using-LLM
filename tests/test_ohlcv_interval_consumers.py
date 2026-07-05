"""interval-less load_ohlcv 残存箇所の掃討 (codex Med#1 同族)。

- performance_audit._load_pair_ohlcv: audit CLI が traded pair の OHLCV を読む経路。
  cache は ohlcv_interval で書かれるため、interval 無指定だと day モードで空になる。
- technical_collector._collect_econ_impact: econ カレンダー反応窓の読み出し。
  tradeable pair の cache は _ohlcv_interval_for (= ohlcv_interval) で書かれる。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd


class _RecordingStore:
    """load_ohlcv に渡された interval を記録する fake PriceStore。"""

    def __init__(self, df=None):
        self.calls = []
        self._df = df if df is not None else pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"]
        )

    def load_ohlcv(self, symbol, start, end, *, interval="1h"):
        self.calls.append((symbol, interval))
        return self._df


# ── performance_audit._load_pair_ohlcv ────────────────────────


def test_audit_load_pair_ohlcv_uses_interval_singleton(monkeypatch):
    """_ohlcv_interval_singleton (run_audit で ohlcv_interval に設定) が伝搬すること。"""
    from src.analysis import performance_audit

    store = _RecordingStore()
    monkeypatch.setattr(performance_audit, "_price_store_singleton", store)
    monkeypatch.setattr(performance_audit, "_ohlcv_interval_singleton", "15m")

    performance_audit._load_pair_ohlcv(
        "USDJPY=X", since=datetime(2026, 7, 1), until=datetime(2026, 7, 2),
    )
    assert store.calls == [("USDJPY=X", "15m")]


def test_audit_load_pair_ohlcv_default_interval_is_1h(monkeypatch):
    """既定 (singleton 未設定相当) は従来通り 1h (挙動不変)。"""
    from src.analysis import performance_audit

    store = _RecordingStore()
    monkeypatch.setattr(performance_audit, "_price_store_singleton", store)
    monkeypatch.setattr(performance_audit, "_ohlcv_interval_singleton", "1h")

    performance_audit._load_pair_ohlcv("USDJPY=X", since=datetime(2026, 7, 1))
    assert store.calls == [("USDJPY=X", "1h")]


# ── technical_collector._collect_econ_impact ──────────────────


async def test_econ_impact_reads_ohlcv_at_trade_interval(monkeypatch):
    """econ 反応窓の load_ohlcv が _ohlcv_interval_for (= ohlcv_interval) で読むこと。

    fake price_store が空 df を返すので pair_reactions は溜まらず、LLM 分析へ
    進まずに mark_analyzed で終わる (interval 伝搬の配線のみを検証する)。
    """
    from src.config import load_config
    from src.jobs import technical_collector

    config = load_config()
    monkeypatch.setattr(config.trading, "ohlcv_interval", "15m")
    monkeypatch.setattr(config.economic_calendar, "enabled", True)

    ev = SimpleNamespace(
        event_id="ev1", currency="USD", event_time=datetime(2026, 7, 1, 21, 30),
    )
    marked = []

    class _FakeEconStore:
        def __init__(self, *a, **k):
            pass

        def get_unanalyzed_with_actual(self, *, lookback_min, min_importance):
            return [ev]

        def mark_analyzed(self, event_id):
            marked.append(event_id)

    import src.data.econ_event_store as econ_event_store_mod
    import src.jobs.econ_calendar_fetcher as econ_calendar_fetcher_mod

    monkeypatch.setattr(econ_event_store_mod, "EconEventStore", _FakeEconStore)
    monkeypatch.setattr(
        econ_calendar_fetcher_mod, "refresh_recent_events", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        technical_collector, "create_llm_client", lambda *a, **k: object(),
    )

    price_store = _RecordingStore()  # 空 df → 反応収集は continue

    class _FakeAnalysisStore:
        def get_recent_ok_snapshots(self, symbol, hours):
            return []

    trade_inst = SimpleNamespace(
        symbol="USDJPY=X", display_name="USD/JPY",
        base_currency="USD", quote_currency="JPY", is_tradeable=True,
    )

    await technical_collector._collect_econ_impact(
        config, store=None, price_store=price_store,
        analysis_store=_FakeAnalysisStore(), tradeable=[trade_inst],
    )

    # 例外が握り潰されて素通りしていないこと (呼び出しが実際に起きたこと) も兼ねる
    assert price_store.calls == [("USDJPY=X", "15m")]
    assert marked == ["ev1"]
