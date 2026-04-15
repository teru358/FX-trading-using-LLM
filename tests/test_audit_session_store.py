"""SessionStore の audit 向けクエリ拡張のテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.data.session_store import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "prices.db")


def _make_session(store: SessionStore, sid: str, opened_at: datetime) -> None:
    store.create_session(
        session_id=sid,
        pair="USDJPY=X",
        direction="buy",
        entry_price=150.0,
        stop_loss=149.5,
        take_profit=151.0,
        position_size=5000,
        signal_score=0.3,
        signal_confidence=0.7,
        macro_context="test",
        analysis_summary="test analysis",
        opened_at=opened_at,
    )


def test_get_closed_sessions_in_range(store):
    """クローズ済みセッションを期間指定で取得できる + closed_at 降順で返る。"""
    now = datetime.now()
    _make_session(store, "s1", now - timedelta(days=5))
    _make_session(store, "s2", now - timedelta(days=3))
    _make_session(store, "s3", now - timedelta(days=1))

    # s1 は days-4 でクローズ、s2 は days-2 でクローズ (s2 の方が新しい)
    store.close_session("s1", now - timedelta(days=4), 151.0, "take_profit", 1000.0)
    store.close_session("s2", now - timedelta(days=2), 149.5, "stop_loss", -500.0)

    since = now - timedelta(days=7)
    until = now
    results = store.get_closed_sessions(since, until)

    # 両方返る
    sids = {r.session_id for r in results}
    assert sids == {"s1", "s2"}
    # 新しい順 (closed_at 降順) で s2 → s1
    assert results[0].session_id == "s2"
    assert results[1].session_id == "s1"


def test_get_closed_sessions_empty(store):
    """該当期間にクローズ済みがなければ空リストを返す。"""
    now = datetime.now()
    results = store.get_closed_sessions(now - timedelta(days=30), now)
    assert results == []


def test_get_closed_sessions_outside_range(store):
    """範囲外のクローズ済みセッションは除外される。"""
    now = datetime.now()
    _make_session(store, "old", now - timedelta(days=60))
    store.close_session("old", now - timedelta(days=59), 151.0, "take_profit", 500.0)

    since = now - timedelta(days=30)
    until = now
    results = store.get_closed_sessions(since, until)
    assert results == []


def test_get_closed_sessions_boundary_inclusive(store):
    """since / until と closed_at が完全に一致するケースも含まれる (inclusive)。"""
    now = datetime.now()
    opened = now - timedelta(days=2)
    closed_exact = now - timedelta(days=1)

    _make_session(store, "boundary", opened)
    store.close_session("boundary", closed_exact, 151.0, "take_profit", 500.0)

    # since = until = closed_exact で取得 → inclusive なので 1 件取れる
    results = store.get_closed_sessions(closed_exact, closed_exact)
    assert len(results) == 1
    assert results[0].session_id == "boundary"


def test_post_hoc_cache_read_write(store):
    """post_hoc_cache カラムに JSON 文字列を保存・取得できる。"""
    import json

    now = datetime.now()
    _make_session(store, "sc1", now - timedelta(days=1))
    store.close_session("sc1", now, 151.0, "take_profit", 1000.0)

    cache_payload = json.dumps({
        "flag": "CLEAN_WIN",
        "mfe_after_close": 100.0,
        "candidates": [],
    })
    store.set_post_hoc_cache("sc1", cache_payload)

    fetched = store.get_post_hoc_cache("sc1")
    assert fetched == cache_payload

    parsed = json.loads(fetched)
    assert parsed["flag"] == "CLEAN_WIN"


def test_post_hoc_cache_missing_returns_none(store):
    """未設定の post_hoc_cache は None を返す。"""
    now = datetime.now()
    _make_session(store, "sc2", now - timedelta(days=1))
    store.close_session("sc2", now, 151.0, "take_profit", 500.0)
    assert store.get_post_hoc_cache("sc2") is None


def test_post_hoc_cache_migration_on_existing_db(tmp_path):
    """既存 DB (post_hoc_cache カラムなし) にも migration で追加される。"""
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trading_sessions ("
            "session_id TEXT PRIMARY KEY, pair TEXT, direction TEXT, "
            "entry_price REAL, stop_loss REAL, take_profit REAL, "
            "position_size REAL, signal_score REAL, signal_confidence REAL, "
            "macro_context TEXT, analysis_summary TEXT, opened_at DATETIME, "
            "closed_at DATETIME, close_price REAL, close_reason TEXT, "
            "realized_pnl REAL, outcome TEXT, reflection_text TEXT, "
            "created_at DATETIME"
            ")"
        ))

    # SessionStore で開くと migration が走る
    store = SessionStore(db_path)

    from sqlalchemy import inspect
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("trading_sessions")}
    assert "post_hoc_cache" in cols
