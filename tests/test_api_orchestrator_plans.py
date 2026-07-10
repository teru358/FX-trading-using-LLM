"""/orchestrator/plans 系 endpoint (gate spec F-5) のテスト。

実 OrchestratorStore (tmp_path SQLite) を state に注入し、TestClient で
一覧 / 詳細 / approve / reject / gate_message / reconcile を検証する。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api._state import state
from src.api.server import app
from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "test-key")


@pytest.fixture
def store(tmp_path: Path):
    s = OrchestratorStore(tmp_path / "orch.db")
    state.orchestrator_store = s
    yield s
    state.orchestrator_store = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _pending_plan(store, *, reasoning: str | None = None) -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status="pending_approval",
    )
    if reasoning:
        store.record_decision(
            run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
            decision_type="plan_create", decision="buy", plan_id=plan_id,
            reasoning_summary=reasoning)
    return plan_id


def test_list_pending_includes_reasoning_and_message_id(store, client):
    plan_id = _pending_plan(store, reasoning="pullback long")
    store.set_gate_message(plan_id, "msg-1")
    res = client.get("/orchestrator/plans", headers=HEADERS)
    assert res.status_code == 200
    rows = res.json()["plans"]
    assert len(rows) == 1
    assert rows[0]["plan_id"] == plan_id
    assert rows[0]["reasoning"] == "pullback long"
    assert rows[0]["gate_message_id"] == "msg-1"
    assert rows[0]["status"] == "pending_approval"


def test_detail_returns_gate_fields(store, client):
    plan_id = _pending_plan(store)
    store.try_decide_gate(plan_id, "rejected", reason="RR悪い")
    res = client.get(f"/orchestrator/plans/{plan_id}", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "rejected"
    assert body["gate_decision"] == "rejected"
    assert body["gate_reason"] == "RR悪い"


def test_detail_404(store, client):
    assert client.get("/orchestrator/plans/999", headers=HEADERS).status_code == 404


def test_approve_then_conflict(store, client):
    plan_id = _pending_plan(store)
    res = client.post(f"/orchestrator/plans/{plan_id}/approve", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "active"
    # 二重決定は 409
    res2 = client.post(f"/orchestrator/plans/{plan_id}/reject", headers=HEADERS)
    assert res2.status_code == 409


def test_reject_persists_reason(store, client):
    plan_id = _pending_plan(store)
    res = client.post(
        f"/orchestrator/plans/{plan_id}/reject",
        headers=HEADERS, json={"reason": "タイミング悪い"})
    assert res.status_code == 200
    assert store.get_trade_plan(plan_id).gate_reason == "タイミング悪い"


def test_gate_message_endpoint(store, client):
    plan_id = _pending_plan(store)
    res = client.post(
        f"/orchestrator/plans/{plan_id}/gate_message",
        headers=HEADERS, json={"message_id": "m-42"})
    assert res.status_code == 200
    assert store.get_trade_plan(plan_id).gate_message_id == "m-42"
    assert client.post(
        "/orchestrator/plans/999/gate_message",
        headers=HEADERS, json={"message_id": "m"}).status_code == 404


def test_reconcile_posted_within_hours(store, client):
    plan_id = _pending_plan(store)
    store.set_gate_message(plan_id, "m-1")
    store.try_decide_gate(plan_id, "rejected")   # pending から消えた投稿済み plan
    res = client.get(
        "/orchestrator/plans?posted_within_hours=24", headers=HEADERS)
    rows = res.json()["plans"]
    assert [r["plan_id"] for r in rows] == [plan_id]
    assert rows[0]["gate_decision"] == "rejected"


def test_auth_required(store, client):
    assert client.get("/orchestrator/plans").status_code in (401, 403, 422)


def test_store_not_configured_returns_503(client):
    state.orchestrator_store = None
    assert client.get("/orchestrator/plans", headers=HEADERS).status_code == 503


def test_out_of_range_plan_id_rejected(store, client):
    huge = 10 ** 30
    assert client.get(
        f"/orchestrator/plans/{huge}", headers=HEADERS).status_code == 422
    assert client.post(
        f"/orchestrator/plans/{huge}/approve", headers=HEADERS).status_code == 422


def test_approve_missing_plan_404(store, client):
    res = client.post("/orchestrator/plans/424242/approve", headers=HEADERS)
    assert res.status_code == 404


def test_reject_reason_too_long_422(store, client):
    plan_id = _pending_plan(store)
    res = client.post(
        f"/orchestrator/plans/{plan_id}/reject",
        headers=HEADERS, json={"reason": "a" * 501})
    assert res.status_code == 422
