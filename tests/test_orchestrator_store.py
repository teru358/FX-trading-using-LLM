"""orchestrator_store の §8 トレーステーブル CRUD テスト。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def test_create_snapshot_returns_id_and_persists(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X",
        as_of_time=datetime(2026, 6, 20, 12, 0, 0),
        quote_json={"bid": 150.0, "ask": 150.02, "mid": 150.01, "spread": 0.02},
        technical_ref={"snapshot_id": 7, "analyzed_at": "2026-06-20T11:45:00"},
        news_ref={"analysis_id": 3, "at": "2026-06-20T11:30:00"},
    )
    assert isinstance(snap_id, int) and snap_id > 0

    snap = store.get_snapshot(snap_id)
    assert snap is not None
    assert snap.pair == "USDJPY=X"
    assert snap.as_of_time == datetime(2026, 6, 20, 12, 0, 0)
    assert snap.quote_json["mid"] == 150.01
    assert snap.technical_ref["snapshot_id"] == 7


def test_agent_run_start_and_finish(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run(
        "PlannerAgent", pair="USDJPY=X", trigger_type="poll",
        snapshot_id=snap_id, model_name="qwen-14b", trade_horizon="swing",
    )
    run = store.get_run(run_id)
    assert run.status == "ok"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.snapshot_id == snap_id

    store.finish_run(run_id, status="failed", error_type="timeout",
                     error_message="llm did not respond")
    run = store.get_run(run_id)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.error_type == "timeout"
