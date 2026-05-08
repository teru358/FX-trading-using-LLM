"""run tech view の表示対象選択テスト。"""
from __future__ import annotations

from datetime import timedelta
import io
from types import SimpleNamespace

from rich.console import Console

from src.analysis.price_analyzer import PriceAnalysis
from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


def _config(*, lookback_hours: int = 8):
    return SimpleNamespace(
        rag=SimpleNamespace(analysis_lookback_hours=lookback_hours),
        watch_only_instruments=[],
        tradeable_instruments=[
            SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY"),
        ],
    )


def _snapshot(symbol: str = "USDJPY=X", *, hours_ago: float = 24) -> PriceAnalysis:
    return PriceAnalysis(
        pair=symbol,
        direction_bias="long",
        bias_score=0.3,
        confidence=0.7,
        entry_zone=(149.5, 150.5),
        stop_loss=149.0,
        take_profit=152.0,
        risk_reward_ratio=2.0,
        reasoning_summary="old but latest trade snapshot",
        analyzed_at=db_now() - timedelta(hours=hours_ago),
    )


def test_run_tech_view_uses_latest_collect_row_even_outside_lookback(
    tmp_path, monkeypatch,
):
    """run_tech_view は lookback 非依存で最新 1 行を取得する (休場中でも見える)。"""
    from src.views import run_tech_view

    store = AnalysisStore(tmp_path / "prices.db")
    store.add_snapshot(_snapshot(hours_ago=24))  # lookback (8h) 外

    captured = {}

    def _capture(snapshots_by_symbol, display_names, lookback_hours):
        captured["snapshots_by_symbol"] = snapshots_by_symbol
        captured["display_names"] = display_names
        captured["lookback_hours"] = lookback_hours

    monkeypatch.setattr("src.views.print_tech_summary", _capture)

    run_tech_view(_config(lookback_hours=8), store)

    snaps = captured["snapshots_by_symbol"]["USDJPY=X"]
    assert len(snaps) == 1
    assert snaps[0].symbol == "USDJPY=X"
    assert captured["display_names"]["USDJPY=X"] == "USD/JPY"


def test_print_tech_summary_marks_snapshot_outside_lookback_as_stale(monkeypatch):
    """lookback 外の表示フォールバックは stale と分かるように表示する。"""
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    snap = SimpleNamespace(
        symbol="USDJPY=X",
        analyzed_at=db_now() - timedelta(hours=24),
        risk_reward_ratio=2.0,
        entry_zone_low=149.5,
        entry_zone_high=150.5,
        stop_loss=149.0,
        take_profit=152.0,
        reasoning_summary="old but latest",
        direction_bias="long",
        bias_score=0.3,
        confidence=0.7,
    )

    reporter.print_tech_summary(
        {"USDJPY=X": [snap]},
        {"USDJPY=X": "USD/JPY"},
        lookback_hours=8,
    )

    assert "stale" in buf.getvalue()
