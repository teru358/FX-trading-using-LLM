"""BalanceSnapshot read/write/bootstrap/refresh_from_mt5 のユニットテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.persistence.balance_snapshot import (
    PAPER_DEFAULT,
    BalanceSnapshot,
    is_stale,
    read,
    refresh_from_mt5,
    update_peak,
    write,
)


def test_read_bootstraps_paper_default_when_missing(tmp_path: Path):
    snap = read(tmp_path)

    assert snap.balance == PAPER_DEFAULT
    assert snap.deposit == PAPER_DEFAULT
    assert snap.peak_balance == PAPER_DEFAULT
    assert snap.source == "paper"
    assert snap.fetched_at  # non-empty ISO string
    # ファイルもディスクに作成される
    assert (tmp_path / "balance.json").exists()


def test_write_then_read_roundtrip(tmp_path: Path):
    snap = BalanceSnapshot(
        balance=12345.0, deposit=10000.0, peak_balance=15000.0,
        source="mt5", fetched_at="2026-05-04T10:30:00+00:00",
    )
    write(tmp_path, snap)
    read_back = read(tmp_path)
    assert read_back == snap


def test_corrupted_json_regenerates_paper(tmp_path: Path):
    p = tmp_path / "balance.json"
    p.write_text("{ invalid json", encoding="utf-8")
    snap = read(tmp_path)
    assert snap.source == "paper"
    assert snap.balance == PAPER_DEFAULT
    # ファイルも上書き済み
    assert json.loads(p.read_text(encoding="utf-8"))["source"] == "paper"


def test_missing_required_field_regenerates_paper(tmp_path: Path):
    p = tmp_path / "balance.json"
    p.write_text(json.dumps({"balance": 99.0}), encoding="utf-8")  # 不完全
    snap = read(tmp_path)
    assert snap.source == "paper"
    assert snap.balance == PAPER_DEFAULT


def test_update_peak_increases_when_new_balance_higher(tmp_path: Path):
    snap = BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=120.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    out = update_peak(snap, 130.0)
    assert out.balance == 130.0
    assert out.peak_balance == 130.0
    assert out.deposit == 100.0  # 不変
    assert out.source == "paper"


def test_update_peak_keeps_peak_when_new_balance_lower():
    snap = BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=120.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    out = update_peak(snap, 80.0)
    assert out.balance == 80.0
    assert out.peak_balance == 120.0  # 維持


def test_refresh_from_mt5_promotes_paper_snapshot():
    snap = BalanceSnapshot(
        balance=10000.0, deposit=10000.0, peak_balance=10000.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    out = refresh_from_mt5(snap, 50000.0)
    assert out.source == "mt5"
    assert out.balance == 50000.0
    assert out.deposit == 50000.0  # 初回 MT5 で deposit 確定
    assert out.peak_balance == 50000.0


def test_refresh_from_mt5_keeps_deposit_after_promotion():
    snap = BalanceSnapshot(
        balance=50000.0, deposit=50000.0, peak_balance=50500.0,
        source="mt5", fetched_at="2026-05-04T10:00:00+00:00",
    )
    out = refresh_from_mt5(snap, 49800.0)
    assert out.balance == 49800.0
    assert out.deposit == 50000.0  # 不変
    assert out.peak_balance == 50500.0  # 上回らないので維持


def test_refresh_from_mt5_updates_peak():
    snap = BalanceSnapshot(
        balance=50000.0, deposit=50000.0, peak_balance=50500.0,
        source="mt5", fetched_at="2026-05-04T10:00:00+00:00",
    )
    out = refresh_from_mt5(snap, 51000.0)
    assert out.peak_balance == 51000.0


def test_is_stale_true_for_old_fetched_at():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
    snap = BalanceSnapshot(
        balance=1.0, deposit=1.0, peak_balance=1.0, source="mt5", fetched_at=old,
    )
    assert is_stale(snap, threshold_minutes=30) is True


def test_is_stale_false_for_fresh_fetched_at():
    from datetime import datetime, timezone
    fresh = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    snap = BalanceSnapshot(
        balance=1.0, deposit=1.0, peak_balance=1.0, source="mt5", fetched_at=fresh,
    )
    assert is_stale(snap, threshold_minutes=30) is False


def test_atomic_write_no_partial_on_existing_file(tmp_path: Path):
    """既存ファイルがあっても tmp+rename で atomic に置換される。"""
    p = tmp_path / "balance.json"
    p.write_text('{"balance": 0}', encoding="utf-8")  # 既存

    snap = BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=100.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    write(tmp_path, snap)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["balance"] == 100.0
    # tmp ファイルは残らない
    assert not (tmp_path / "balance.json.tmp").exists()
