"""migration の冪等性テスト (spec §4)。"""
import sqlite3

from scripts.migrate_cycle_retirement import delete_adaptive_params, drop_retired_tables


def _make_db(tmp_path):
    db = tmp_path / "prices.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hold_decisions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE trading_sessions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE technical_snapshots (id INTEGER PRIMARY KEY)")
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
