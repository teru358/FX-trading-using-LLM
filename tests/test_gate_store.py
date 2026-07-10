"""approval gate の store 層テスト (spec 2026-07-05-discord-approval-gate.md F-1〜F-4)。

status/列の存在・gate 遷移 helper・stamp/latch/finalize の claim 意味論を
実 SQLite (tmp_path) で検証する。時刻は db_now() 相対 (date-flake 防止)。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def _plan(store, *, status="pending_approval", pair="USDJPY=X",
          expires_at=None, **kw) -> int:
    """gate テスト用 plan seed。status 指定で直接 ORM 経由 seed する。"""
    snap = store.create_snapshot(pair=pair, as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair=pair)
    return store.create_trade_plan(
        pair=pair, snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=[],
        expires_at=expires_at or (db_now() + timedelta(hours=8)),
        created_by_run_id=run_id, status=status, **kw,
    )


# ── Task 1: status / 列 ────────────────────────────────────────


def test_pending_approval_status_accepted(store):
    plan_id = _plan(store, status="pending_approval")
    assert store.get_trade_plan(plan_id).status == "pending_approval"


def test_rejected_status_accepted(store):
    plan_id = _plan(store, status="pending_approval")
    store.update_plan_status(plan_id, "rejected")
    assert store.get_trade_plan(plan_id).status == "rejected"


def test_gate_and_cf_columns_default_null(store):
    plan = store.get_trade_plan(_plan(store))
    assert plan.gate_decision is None
    assert plan.gate_decided_at is None
    assert plan.gate_reason is None
    assert plan.gate_message_id is None
    assert plan.cf_state is None
    assert plan.cf_stamped_at is None
    assert plan.cf_stamp_price is None
    assert plan.cf_stamp_spread_pips is None


def test_migration_idempotent(tmp_path):
    """同一 DB に対する二重構築で ALTER が落ちない (idempotent)。"""
    db = tmp_path / "orch.db"
    OrchestratorStore(db)
    store2 = OrchestratorStore(db)  # 2回目 — 既存列で例外にならない
    assert store2.get_trade_plan(999) is None  # 通常操作が生きている


# ── Task 2: try_decide_gate ───────────────────────────────────


def test_decide_gate_approve_sets_all_fields(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "active"
    assert plan.gate_decision == "approved"
    assert plan.gate_decided_at is not None
    assert plan.gate_reason is None


def test_decide_gate_reject_with_reason(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "rejected", reason="RR悪い") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "rejected"
    assert plan.gate_decision == "rejected"
    assert plan.gate_reason == "RR悪い"


def test_decide_gate_loses_when_not_pending(store):
    """既に決定済み (active) の plan への二重決定は False (API 層で 409)。"""
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    assert store.try_decide_gate(plan_id, "rejected") is False
    # 先勝ちの結果が保持される
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "active"
    assert plan.gate_decision == "approved"


def test_decide_gate_invalid_decision_raises(store):
    plan_id = _plan(store)
    with pytest.raises(ValueError):
        store.try_decide_gate(plan_id, "maybe")
