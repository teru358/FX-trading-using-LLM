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


# ── Task 3: unanswered 終端 / stamp / latch ────────────────────


def test_close_pending_unanswered_expired(store):
    plan_id = _plan(store)
    assert store.try_close_pending_unanswered(plan_id, "expired") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert plan.gate_decided_at is None  # 放置は決定時刻なし


def test_close_pending_unanswered_invalidated(store):
    """invalidation による判断なし終端も unanswered (G-3 拡張解釈・内訳は status)。"""
    plan_id = _plan(store)
    assert store.try_close_pending_unanswered(plan_id, "invalidated") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "invalidated"
    assert plan.gate_decision == "unanswered"


def test_close_pending_loses_to_approve_race(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    assert store.try_close_pending_unanswered(plan_id, "expired") is False
    assert store.get_trade_plan(plan_id).status == "active"


def test_close_pending_invalid_status_raises(store):
    with pytest.raises(ValueError):
        store.try_close_pending_unanswered(_plan(store), "superseded")


def test_stamp_would_trigger_once(store):
    plan_id = _plan(store)
    now = db_now()
    assert store.try_stamp_would_trigger(
        plan_id, at=now, price=150.25, spread_pips=1.2) is True
    plan = store.get_trade_plan(plan_id)
    assert plan.cf_state == "would_trigger"
    assert plan.cf_stamp_price == 150.25
    assert plan.cf_stamp_spread_pips == 1.2
    # dedupe: 2回目は負ける (最初の成立瞬間がエントリー点)
    assert store.try_stamp_would_trigger(
        plan_id, at=now, price=151.0, spread_pips=2.0) is False
    assert store.get_trade_plan(plan_id).cf_stamp_price == 150.25


def test_stamp_requires_pending_status(store):
    """active plan (承認済み) には stamp しない (real 経路の領域)。"""
    plan_id = _plan(store, status="active")
    assert store.try_stamp_would_trigger(
        plan_id, at=db_now(), price=150.0, spread_pips=1.0) is False


def test_latch_cf_invalidated_on_rejected(store):
    plan_id = _plan(store)
    store.try_decide_gate(plan_id, "rejected")
    assert store.try_latch_cf_invalidated(plan_id) is True
    assert store.get_trade_plan(plan_id).cf_state == "invalidated"
    # 冪等: 2回目は負け
    assert store.try_latch_cf_invalidated(plan_id) is False


def test_latch_requires_rejected_status(store):
    plan_id = _plan(store)  # pending のまま
    assert store.try_latch_cf_invalidated(plan_id) is False
