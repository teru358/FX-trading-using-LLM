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


def test_create_trade_plan_and_update_status(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    plan_id = store.create_trade_plan(
        pair="USDJPY=X",
        snapshot_id=snap_id,
        horizon="swing",
        direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.8, "tp": 151.5, "size_policy": "risk_pct"},
        invalidation_json=[{"type": "price_below", "value": 149.80}],
        expires_at=datetime(2026, 6, 27, 12, 0, 0),
        created_by_run_id=1,
    )
    assert isinstance(plan_id, int)

    plan = store.get_trade_plan(plan_id)
    assert plan.status == "active"
    assert plan.direction == "long"
    assert plan.entry_conditions_json[0]["value"] == 150.30

    store.update_plan_status(plan_id, "suspended")
    assert store.get_trade_plan(plan_id).status == "suspended"


def test_get_active_plans_filters_non_active(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    active = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap_id, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=1,
    )
    suspended = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap_id, horizon="swing", direction="short",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=1,
    )
    store.update_plan_status(suspended, "suspended")

    active_plans = store.get_active_plans("USDJPY=X")
    ids = {p.plan_id for p in active_plans}
    assert active in ids
    assert suspended not in ids


def test_order_intent_insert_and_duplicate_plan_id_rejected(store: OrchestratorStore) -> None:
    ok = store.try_insert_order_intent(
        plan_id=42, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 12, 2, 0),
    )
    assert ok is True

    # 同一 plan_id の 2 回目は UNIQUE 違反 → False (= 既発注として中止)
    dup = store.try_insert_order_intent(
        plan_id=42, pair="USDJPY=X", intended_action="buy",
        owner_run_id=2, lease_until=datetime(2026, 6, 20, 12, 4, 0),
    )
    assert dup is False


def test_order_intent_mark_submitted_and_result(store: OrchestratorStore) -> None:
    store.try_insert_order_intent(
        plan_id=7, pair="USDJPY=X", intended_action="sell",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 12, 2, 0),
    )
    store.mark_order_submitted(plan_id=7, submitted_at=datetime(2026, 6, 20, 12, 1, 30))
    store.record_order_result(
        plan_id=7, status="filled", order_id="MT5-123",
        broker_result_json={"order_id": "MT5-123", "filled": True},
    )
    intent = store.get_order_intent(plan_id=7)
    assert intent.status == "filled"
    assert intent.order_id == "MT5-123"
    assert intent.submitted_at == datetime(2026, 6, 20, 12, 1, 30)


def test_order_intent_persists_recovery_columns(store: OrchestratorStore) -> None:
    """pending INSERT 時に recovery 系カラムが正しい初期値で保存される。

    later plan の recovery job が判別に使うため、INSERT 直後の状態を固定する:
    owner_run_id / lease_until が保存され、submitted_at は None、
    recovery_status は None (= 未判定)。
    """
    store.try_insert_order_intent(
        plan_id=11, pair="USDJPY=X", intended_action="buy",
        owner_run_id=99, lease_until=datetime(2026, 6, 20, 12, 2, 0),
    )
    intent = store.get_order_intent(plan_id=11)
    assert intent.status == "pending"
    assert intent.owner_run_id == 99
    assert intent.lease_until == datetime(2026, 6, 20, 12, 2, 0)
    assert intent.submitted_at is None      # broker 送信前 = 送信前/後の分岐点
    assert intent.recovery_status is None   # まだ recovery 判定されていない


def test_get_stale_pending_intents_detects_lease_expired(store: OrchestratorStore) -> None:
    """lease_until を過ぎた pending 行を recovery job が拾えることを検証する。

    pending のまま放置されると plan_id UNIQUE が発注を永久停止するため、
    TTL 超過 pending を列挙できることが復旧設計の前提 (§8.8)。
    """
    # lease 失効済みの pending (拾われるべき)
    store.try_insert_order_intent(
        plan_id=21, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 12, 0, 0),
    )
    # lease がまだ有効な pending (拾われない)
    store.try_insert_order_intent(
        plan_id=22, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 23, 59, 0),
    )
    # 既に submitted (pending ではない → 拾われない)
    store.try_insert_order_intent(
        plan_id=23, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 12, 0, 0),
    )
    store.mark_order_submitted(plan_id=23, submitted_at=datetime(2026, 6, 20, 12, 1, 0))

    stale = store.get_stale_pending_intents(now=datetime(2026, 6, 20, 12, 30, 0))
    stale_plan_ids = {i.plan_id for i in stale}
    assert 21 in stale_plan_ids
    assert 22 not in stale_plan_ids
    assert 23 not in stale_plan_ids


def test_update_plan_status_rejects_unknown_status(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap_id, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=1,
    )
    with pytest.raises(ValueError):
        store.update_plan_status(plan_id, "bogus_status")


def test_record_order_result_rejects_unknown_status(store: OrchestratorStore) -> None:
    store.try_insert_order_intent(
        plan_id=51, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=datetime(2026, 6, 20, 12, 2, 0),
    )
    with pytest.raises(ValueError):
        store.record_order_result(plan_id=51, status="bogus_status")
