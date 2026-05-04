"""BalanceSnapshot read/write/bootstrap/refresh_from_mt5 のユニットテスト。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.persistence.balance_snapshot import (
    PAPER_DEFAULT,
    BalanceSnapshot,
    is_stale,
    mutate,
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
    old = (datetime.now(tz=timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
    snap = BalanceSnapshot(
        balance=1.0, deposit=1.0, peak_balance=1.0, source="mt5", fetched_at=old,
    )
    assert is_stale(snap, threshold_minutes=30) is True


def test_is_stale_false_for_fresh_fetched_at():
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


def test_mutate_returns_updated_snapshot_and_persists(tmp_path: Path):
    """mutate(fn) でロック下に read → fn → write を行い、結果を返す。"""
    write(tmp_path, BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=100.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    ))

    out = mutate(tmp_path, lambda snap: update_peak(snap, 150.0))

    assert out.balance == 150.0
    assert out.peak_balance == 150.0
    # ディスクにも反映されている
    on_disk = read(tmp_path)
    assert on_disk.balance == 150.0


def test_invalid_source_value_regenerates_paper(tmp_path: Path):
    """source が 'paper'|'mt5' 以外なら paper bootstrap で復旧。"""
    p = tmp_path / "balance.json"
    p.write_text(json.dumps({
        "balance": 50.0, "deposit": 50.0, "peak_balance": 50.0,
        "source": "garbage", "fetched_at": "2026-05-04T10:00:00+00:00",
    }), encoding="utf-8")
    snap = read(tmp_path)
    assert snap.source == "paper"
    assert snap.balance == PAPER_DEFAULT


def test_balance_snapshot_rejects_invalid_source():
    """直接コンストラクト時も invalid source は ValueError。"""
    with pytest.raises(ValueError, match="invalid source"):
        BalanceSnapshot(
            balance=1.0, deposit=1.0, peak_balance=1.0,
            source="garbage", fetched_at="2026-05-04T10:00:00+00:00",
        )


def test_update_peak_rejects_nan():
    snap = BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=120.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    with pytest.raises(ValueError, match="must be finite"):
        update_peak(snap, float("nan"))


def test_refresh_from_mt5_rejects_inf():
    snap = BalanceSnapshot(
        balance=100.0, deposit=100.0, peak_balance=100.0,
        source="paper", fetched_at="2026-05-04T10:00:00+00:00",
    )
    with pytest.raises(ValueError, match="must be finite"):
        refresh_from_mt5(snap, float("inf"))
