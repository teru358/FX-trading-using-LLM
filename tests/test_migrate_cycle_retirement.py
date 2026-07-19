"""migration の冪等性 + dry-run 既定テスト (spec §4)。"""
import sqlite3

import pytest

from scripts.migrate_cycle_retirement import (
    build_parser,
    delete_adaptive_params,
    drop_retired_tables,
    inspect_retired_tables,
    render_dry_run,
)


def _make_db(tmp_path):
    db = tmp_path / "prices.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hold_decisions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE trading_sessions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE technical_snapshots (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO forecasts (id) VALUES (?)", [(1,), (2,), (3,)])
    conn.executemany("INSERT INTO hold_decisions (id) VALUES (?)", [(1,), (2,)])
    conn.execute("INSERT INTO technical_snapshots (id) VALUES (9)")
    conn.commit()
    conn.close()
    return db


def _tables(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_drops_only_retired_tables(tmp_path):
    db = _make_db(tmp_path)
    dropped = drop_retired_tables(db)
    assert dropped == ["forecasts", "hold_decisions", "trading_sessions"]
    assert _tables(db) == {"technical_snapshots"}


def test_drop_idempotent(tmp_path):
    db = _make_db(tmp_path)
    drop_retired_tables(db)
    assert drop_retired_tables(db) == []


def test_delete_adaptive_params(tmp_path):
    # 実ファイル名は adaptive_params.yaml (adaptive_params_store.py:11 _FILENAME)
    f = tmp_path / "adaptive_params.yaml"
    f.write_text("{}")
    assert delete_adaptive_params(tmp_path) is True
    assert not f.exists()
    assert delete_adaptive_params(tmp_path) is False   # 冪等


# --- dry-run 既定 (plan からの意図的逸脱: 破壊的操作の可視化) ---


def _snapshot(db):
    """テーブル集合 + 各テーブル全行を取る (副作用検知用)。"""
    conn = sqlite3.connect(db)
    try:
        names = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        return {n: conn.execute(f"SELECT * FROM {n}").fetchall() for n in names}
    finally:
        conn.close()


def test_inspect_reports_row_counts_without_dropping(tmp_path):
    db = _make_db(tmp_path)
    before = _snapshot(db)

    report = inspect_retired_tables(db)

    assert report["present"] == {
        "forecasts": 3, "hold_decisions": 2, "trading_sessions": 0}
    assert report["missing"] == []
    assert report["preserved"] == {"technical_snapshots": 1}
    assert _snapshot(db) == before   # 一切の副作用なし


def test_inspect_reports_already_migrated_tables(tmp_path):
    db = _make_db(tmp_path)
    drop_retired_tables(db)

    report = inspect_retired_tables(db)

    assert report["present"] == {}
    assert report["missing"] == ["forecasts", "hold_decisions", "trading_sessions"]


def test_inspect_uses_readonly_connection(tmp_path):
    """read-only URI 接続なので、書き込みを試みても DB は変更できない。"""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE forecasts")
    finally:
        conn.close()
    assert "forecasts" in _tables(db)


def test_dry_run_does_not_modify_db_or_files(tmp_path):
    db = _make_db(tmp_path)
    params = tmp_path / "adaptive_params.yaml"
    params.write_text("{}")
    before = _snapshot(db)

    render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)

    assert _snapshot(db) == before
    assert params.exists()


def test_dry_run_output_lists_tables_with_row_counts(tmp_path):
    db = _make_db(tmp_path)
    (tmp_path / "adaptive_params.yaml").write_text("{}")

    out = render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)

    assert "forecasts" in out and "3" in out
    assert "hold_decisions" in out and "2" in out
    assert "trading_sessions" in out
    assert "technical_snapshots" in out          # 温存テーブルの提示
    assert "adaptive_params.yaml" in out
    assert "--execute" in out                    # 実行方法の案内


def test_dry_run_output_reports_rag_counts_when_available(tmp_path):
    db = _make_db(tmp_path)
    out = render_dry_run(
        db_path=db, state_dir=tmp_path, rag_counts={"bullish": 7, "bearish": 4})
    assert "7" in out and "4" in out


def test_dry_run_output_notes_rag_unavailable(tmp_path):
    db = _make_db(tmp_path)
    out = render_dry_run(db_path=db, state_dir=tmp_path, rag_counts=None)
    assert "実行時に判明" in out


def test_parser_defaults_to_dry_run():
    assert build_parser().parse_args([]).execute is False


def test_parser_execute_flag():
    assert build_parser().parse_args(["--execute"]).execute is True
