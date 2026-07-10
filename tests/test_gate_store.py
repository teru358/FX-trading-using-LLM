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


# ── Task 4: finalize_cf_trigger ───────────────────────────────


def _stamped_rejected(store) -> int:
    """stamp 済みで却下された plan (finalize 対象の代表形) を作る。

    stamp は 2h 前 — horizon 3600s の評価期限を既に過ぎており、finalize 後の
    hindsight「即 ready」assert の時刻条件を満たす (codex plan review Medium)。
    """
    plan_id = _plan(store)
    store.try_stamp_would_trigger(
        plan_id, at=db_now() - timedelta(hours=2), price=150.25, spread_pips=1.2)
    store.try_decide_gate(plan_id, "rejected")
    return plan_id


def _run(store) -> int:
    return store.start_run("OrchestratorRuntime", pair="USDJPY=X")


def test_finalize_from_stamp_writes_row_decision_and_hindsight(store):
    plan_id = _stamped_rejected(store)
    trig_id = store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=True, horizon_seconds=3600)
    assert trig_id is not None
    plan = store.get_trade_plan(plan_id)
    assert plan.cf_state == "triggered"
    trig = store.get_shadow_trigger(plan_id)
    assert trig.trigger_price == 150.25          # stamp 値が使われる
    assert trig.spread_pips == 1.2
    assert trig.triggered_at == plan.cf_stamped_at
    # decision も同一 tx で書かれ trigger と紐づく (claim 負けで偽 decision を残さない)
    dec = store.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_cf_trigger"
    assert dec.plan_id == plan_id
    # hindsight が enqueue され、過去時刻 triggered_at (2h 前) なので即 ready になる
    ready = store.get_pending_hindsight_evaluations(now=db_now())
    assert any(ev.shadow_trigger_id == trig_id for ev in ready)


def test_finalize_direct_uses_args(store):
    """stamp なし rejected の直接記録 (from=NULL claim、gate_decision='rejected' 限定)。"""
    plan_id = _plan(store)
    store.try_decide_gate(plan_id, "rejected")
    now = db_now()
    trig_id = store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False, horizon_seconds=3600,
        triggered_at=now, trigger_price=149.9, spread_pips=0.8)
    assert trig_id is not None
    trig = store.get_shadow_trigger(plan_id)
    assert trig.trigger_price == 149.9
    assert store.get_trade_plan(plan_id).cf_state == "triggered"


def test_finalize_idempotent_second_call_loses(store):
    plan_id = _stamped_rejected(store)
    store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False, horizon_seconds=3600)
    assert store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False,
        horizon_seconds=3600) is None


def test_finalize_refuses_non_terminal_plan(store):
    """pending (承認可能) な plan には絶対に cf 行を書かない (UNIQUE 保護)。"""
    plan_id = _plan(store)
    store.try_stamp_would_trigger(
        plan_id, at=db_now() - timedelta(hours=2), price=150.0, spread_pips=1.0)
    assert store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False,
        horizon_seconds=3600) is None
    assert store.get_shadow_trigger(plan_id) is None


def test_finalize_refuses_approved_plan(store):
    """承認済み plan は terminal になっても claim 不可 (codex plan review High#1)。

    stamp→approve→real trigger→live 発注失敗で invalidated、の経路で
    「terminal + would_trigger」に一致しても gate_decision='approved' が排除する。
    """
    plan_id = _plan(store)
    store.try_stamp_would_trigger(
        plan_id, at=db_now() - timedelta(hours=2), price=150.0, spread_pips=1.0)
    store.try_decide_gate(plan_id, "approved")
    store.update_plan_status(plan_id, "invalidated")  # 発注失敗経路の simulate
    assert store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False,
        horizon_seconds=3600) is None
    assert store.get_trade_plan(plan_id).cf_state == "would_trigger"  # 状態不変


def test_finalize_unique_conflict_leaves_state_untouched(store):
    """行が既にある病的状態 → 状態を進めず None を返す (codex plan review High#2)。

    無条件で cf_state を進めると「state だけ進んだ孤児」を再発させるため、
    rollback + 整合性エラーログに倒す。
    """
    plan_id = _stamped_rejected(store)
    plan = store.get_trade_plan(plan_id)
    store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair=plan.pair, direction=plan.direction,
        triggered_at=plan.cf_stamped_at, trigger_price=plan.cf_stamp_price)
    assert store.finalize_cf_trigger(
        plan_id, run_id=_run(store), enqueue_hindsight=False,
        horizon_seconds=3600) is None
    assert store.get_trade_plan(plan_id).cf_state == "would_trigger"  # 前進しない


# ── Task 5: watch-set queries ─────────────────────────────────


def test_watch_cf_plans_includes_pending_and_windowed_rejected(store):
    p_pending = _plan(store)
    p_rejected = _plan(store)
    store.try_decide_gate(p_rejected, "rejected")
    ids = {p.plan_id for p in store.get_watch_cf_plans("USDJPY=X")}
    assert ids == {p_pending, p_rejected}


def test_watch_cf_plans_excludes_resolved_and_out_of_window(store):
    # cf 解決済み rejected → 恒久的に外れる
    p_done = _plan(store)
    store.try_decide_gate(p_done, "rejected")
    store.finalize_cf_trigger(
        p_done, run_id=_run(store), enqueue_hindsight=False, horizon_seconds=3600,
        triggered_at=db_now(), trigger_price=150.0, spread_pips=1.0)
    # latch 済み rejected → 外れる
    p_latched = _plan(store)
    store.try_decide_gate(p_latched, "rejected")
    store.try_latch_cf_invalidated(p_latched)
    # 窓外 (expires_at 過去) rejected → 外れる
    p_old = _plan(store, expires_at=db_now() - timedelta(hours=1))
    store.try_decide_gate(p_old, "rejected")
    # stamp 済み pending は残る (expiry/invalidation 遷移の責務があるため)
    p_stamped = _plan(store)
    store.try_stamp_would_trigger(
        p_stamped, at=db_now(), price=150.0, spread_pips=1.0)
    ids = {p.plan_id for p in store.get_watch_cf_plans("USDJPY=X")}
    assert ids == {p_stamped}


def test_cf_finalize_pending_returns_crashed_terminals(store):
    """status 遷移後・finalize 前にクラッシュした plan を回収対象として返す。"""
    p1 = _plan(store)
    store.try_stamp_would_trigger(p1, at=db_now(), price=150.0, spread_pips=1.0)
    store.try_decide_gate(p1, "rejected")     # rejected + would_trigger (finalize 前)
    p2 = _plan(store)
    store.try_stamp_would_trigger(p2, at=db_now(), price=150.1, spread_pips=1.0)
    store.try_close_pending_unanswered(p2, "expired")  # expired + would_trigger
    ids = {p.plan_id for p in store.get_cf_finalize_pending("USDJPY=X")}
    assert ids == {p1, p2}
    # finalize 後は集合から抜ける
    store.finalize_cf_trigger(
        p1, run_id=_run(store), enqueue_hindsight=False, horizon_seconds=3600)
    ids = {p.plan_id for p in store.get_cf_finalize_pending("USDJPY=X")}
    assert ids == {p2}


def test_cf_finalize_pending_excludes_approved(store):
    """承認済み plan が発注失敗等で invalidated になっても回収対象にしない
    (codex plan review High#1 — gate_decision 条件の検証)。"""
    p = _plan(store)
    store.try_stamp_would_trigger(p, at=db_now(), price=150.0, spread_pips=1.0)
    store.try_decide_gate(p, "approved")
    store.update_plan_status(p, "invalidated")   # 発注失敗経路の simulate
    assert store.get_cf_finalize_pending("USDJPY=X") == []


# ── Task 7: supersede 拡張 ────────────────────────────────────


def test_supersede_includes_pending_approval(store):
    """新 plan 作成時、返答待ち plan も置換される (G-6)。判断なし = gate_decision NULL。"""
    p_pending = _plan(store)
    ids = store.supersede_active_plans("USDJPY=X")
    assert ids == [p_pending]
    plan = store.get_trade_plan(p_pending)
    assert plan.status == "superseded"
    assert plan.gate_decision is None  # rejected ではない (人間の判断なし)
    assert plan.cf_state == "superseded"  # F-7 集計マーカー (gate OFF と区別する唯一の手段)


def test_supersede_active_plan_no_cf_marker(store):
    """active (gate OFF/approved) の置換には cf マーカーを付けない。"""
    p_active = _plan(store, status="active")
    store.supersede_active_plans("USDJPY=X")
    assert store.get_trade_plan(p_active).cf_state is None


# ── Task 13: F-5 補助 (reasoning JOIN / gate_message / reconcile) ──


def test_latest_plan_create_reasoning(store):
    plan_id = _plan(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    store.record_decision(
        run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
        decision_type="plan_create", decision="buy", plan_id=plan_id,
        reasoning_summary="古い理由")
    store.record_decision(
        run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
        decision_type="plan_create", decision="buy", plan_id=plan_id,
        reasoning_summary="最新の理由")
    assert store.get_latest_plan_create_reasoning(plan_id) == "最新の理由"


def test_latest_reasoning_none_when_missing(store):
    assert store.get_latest_plan_create_reasoning(_plan(store)) is None


def test_set_gate_message_idempotent(store):
    plan_id = _plan(store)
    assert store.set_gate_message(plan_id, "msg-123") is True
    assert store.set_gate_message(plan_id, "msg-123") is True  # 冪等
    assert store.get_trade_plan(plan_id).gate_message_id == "msg-123"
    assert store.set_gate_message(99999, "msg-x") is False     # 404 相当


def test_gate_posted_plans_window_and_any_status(store):
    p1 = _plan(store)
    store.set_gate_message(p1, "m1")
    store.try_decide_gate(p1, "rejected")     # 非 pending でも返る (status 不問)
    p2 = _plan(store)                          # 未投稿 → 返らない
    p3 = _plan(store)
    store.set_gate_message(p3, "m3")
    rows = store.get_gate_posted_plans(within_hours=24)
    ids = {p.plan_id for p in rows}
    assert ids == {p1, p3}
    assert p2 not in ids
