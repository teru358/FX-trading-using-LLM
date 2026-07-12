"""run tech view の表示対象選択テスト。"""
from __future__ import annotations

from datetime import timedelta
import io
from types import SimpleNamespace

from rich.console import Console

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
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


def _snapshot(symbol: str = "USDJPY=X", *, hours_ago: float = 24) -> TechnicalSnapshotData:
    return TechnicalSnapshotData(
        pair=symbol,
        direction_bias="long",
        bias_score=0.3,
        confidence=0.7,
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

    def _capture(rows):
        captured["rows"] = rows

    monkeypatch.setattr("src.views.print_tech_summary", _capture)

    run_tech_view(_config(lookback_hours=8), store)

    rows = captured["rows"]
    assert len(rows) == 1
    inst, latest_collect, latest_ok = rows[0]
    assert inst.symbol == "USDJPY=X"
    assert latest_collect is not None
    assert latest_collect.symbol == "USDJPY=X"
    assert latest_ok is not None
    assert latest_ok.collect_status == "ok"


def test_print_tech_summary_shows_collect_status_and_latest_ok(monkeypatch):
    """sentinel 最新 + 古い ok → Status は sentinel、Bias 列は ok 値。"""
    import io
    from datetime import timedelta
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    latest_collect = SimpleNamespace(
        analyzed_at=db_now() - timedelta(minutes=5),
        collect_status="stale_price",
        reasoning_summary="latest bar 7:00:00 ago",
    )
    latest_ok = SimpleNamespace(
        analyzed_at=db_now() - timedelta(hours=4),
        collect_status="ok",
        direction_bias="long",
        bias_score=0.12,
        confidence=0.65,
    )

    reporter.print_tech_summary([(inst, latest_collect, latest_ok)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "stale_price" in output
    assert "long" in output
    assert "0.12" in output
    # Glyph assertion — must appear in BOTH the status cell and the legend.
    # When _STATUS_GLYPH is intact, count >= 2 (cell + legend).
    # When _STATUS_GLYPH is empty, the cell falls back to "? stale_price" and
    # the glyph appears only once (in the legend).
    assert output.count("⚠") >= 2  # stale_price glyph in cell + legend


def test_print_tech_summary_no_data(monkeypatch):
    """latest_collect=None, latest_ok=None → '(no data)' 表示。"""
    import io
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    reporter.print_tech_summary([(inst, None, None)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "no data" in output


def test_print_tech_summary_only_sentinel(monkeypatch):
    """sentinel あり、ok 無し → Status 表示、Bias 列は '—'。"""
    import io
    from datetime import timedelta
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    latest_collect = SimpleNamespace(
        analyzed_at=db_now() - timedelta(minutes=10),
        collect_status="failed",
        reasoning_summary="llm_error: TimeoutError",
    )

    reporter.print_tech_summary([(inst, latest_collect, None)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "failed" in output
    assert "no recent ok" in output
    # Glyph assertion — must appear in BOTH the status cell and the legend.
    # When _STATUS_GLYPH is empty, only the legend has it (count == 1).
    assert output.count("✗") >= 2  # failed glyph in cell + legend


def test_run_tech_view_separates_latest_collect_and_latest_ok(
    tmp_path, monkeypatch,
):
    """sentinel 最新 + 古い ok → latest_collect は sentinel、latest_ok は古い ok。"""
    from src.views import run_tech_view

    store = AnalysisStore(tmp_path / "prices.db")
    store.add_snapshot(_snapshot(hours_ago=4))           # 古い ok
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="recent fail")

    captured = {}
    monkeypatch.setattr(
        "src.views.print_tech_summary",
        lambda rows: captured.setdefault("rows", rows),
    )
    run_tech_view(_config(lookback_hours=8), store)

    rows = captured["rows"]
    assert len(rows) == 1
    _inst, latest_collect, latest_ok = rows[0]
    assert latest_collect.collect_status == "stale_price"
    assert latest_ok.collect_status == "ok"
