"""create_broker factory のユニットテスト (Phase 3c balance.json 同期後)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.persistence import balance_snapshot
from src.persistence.balance_snapshot import OBSERVER_DEFAULT, BalanceSnapshot
from src.trading.live_broker import create_broker


def test_create_broker_live_test_seeds_observer_balance(tmp_path: Path):
    """live_test モード初回構築で observer balance.json が OBSERVER_DEFAULT で seed される。"""
    obs_dir = tmp_path / "shadow_state"

    create_broker(
        "live_test",
        "mt5",
        mt5_bridge_url="http://example.local:8812",
        live_test_observer_state_dir=str(obs_dir),
        live_test_log_path=str(tmp_path / "shadow.jsonl"),
        state_dir=tmp_path,
    )

    snap = balance_snapshot.read(obs_dir)
    assert snap.balance == OBSERVER_DEFAULT
    assert snap.deposit == OBSERVER_DEFAULT
    assert snap.peak_balance == OBSERVER_DEFAULT
    assert snap.source == "paper"


def test_create_broker_live_test_reseeds_drifted_observer(tmp_path: Path):
    """observer balance drift は restart 毎にリセット (Phase 3c 暫定挙動)。"""
    obs_dir = tmp_path / "shadow_state"
    obs_dir.mkdir(parents=True)
    # ¥100,050 に drift した状態を作る
    balance_snapshot.write(obs_dir, BalanceSnapshot(
        balance=100_050.0, deposit=OBSERVER_DEFAULT, peak_balance=100_050.0,
        source="paper", fetched_at="2026-05-04T00:00:00+00:00",
    ))

    create_broker(
        "live_test",
        "mt5",
        mt5_bridge_url="http://example.local:8812",
        live_test_observer_state_dir=str(obs_dir),
        live_test_log_path=str(tmp_path / "shadow.jsonl"),
        state_dir=tmp_path,
    )

    snap = balance_snapshot.read(obs_dir)
    assert snap.balance == OBSERVER_DEFAULT  # drift discarded
    assert snap.peak_balance == OBSERVER_DEFAULT
