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
    """クローズ済みセッションを期間指定で取得できる。"""
    now = datetime.now()
    _make_session(store, "s1", now - timedelta(days=5))
    _make_session(store, "s2", now - timedelta(days=3))
    _make_session(store, "s3", now - timedelta(days=1))

    store.close_session("s1", now - timedelta(days=4), 151.0, "take_profit", 1000.0)
    store.close_session("s2", now - timedelta(days=2), 149.5, "stop_loss", -500.0)

    since = now - timedelta(days=7)
    until = now
    results = store.get_closed_sessions(since, until)
    sids = sorted(r.session_id for r in results)
    assert sids == ["s1", "s2"]


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
