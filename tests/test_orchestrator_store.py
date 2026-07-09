"""orchestrator_store の §8 トレーステーブル CRUD テスト。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore, _TradePlan
from sqlalchemy.orm import Session


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


def test_record_decision_and_freshness(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X", snapshot_id=snap_id)

    decision_id = store.record_decision(
        run_id=run_id,
        snapshot_id=snap_id,
        pair="USDJPY=X",
        decision_type="direct_hold",
        decision="hold",
        reasoning_summary="no opportunity",
        trade_horizon="swing",
    )
    assert isinstance(decision_id, int)

    store.record_freshness(
        snapshot_id=snap_id,
        pair="USDJPY=X",
        price_age_sec=12.0,
        technical_age_sec=900.0,
        news_age_sec=1800.0,
        rag_case_count=3,
        issues=[],
        decision_id=decision_id,
    )

    dec = store.get_decision(decision_id)
    assert dec.decision_type == "direct_hold"
    assert dec.decision == "hold"
    assert dec.plan_id is None

    fresh = store.get_freshness_for_snapshot(snap_id)
    assert fresh.price_age_sec == 12.0
    assert fresh.decision_id == decision_id


def test_record_vote_trace(store: OrchestratorStore) -> None:
    snap_id = store.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0))
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X", snapshot_id=snap_id)
    decision_id = store.record_decision(
        run_id=run_id, snapshot_id=snap_id, pair="USDJPY=X",
        decision_type="direct_hold", decision="hold",
    )
    store.record_vote(
        decision_id=decision_id, agent_run_id=run_id, agent_name="TechnicalAgent",
        vote_action="hold", vote_score=0.1, vote_confidence=0.4, reflected_in_plan=False,
    )
    votes = store.get_votes(decision_id)
    assert len(votes) == 1
    assert votes[0].agent_name == "TechnicalAgent"
    assert votes[0].reflected_in_plan is False


def test_record_decision_rejects_unknown_type(store: OrchestratorStore) -> None:
    """未知の decision_type は ValueError (Tasks 1-2 の status 検証と同じ規約)。"""
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X", snapshot_id=snap_id)
    with pytest.raises(ValueError):
        store.record_decision(
            run_id=run_id, snapshot_id=snap_id, pair="USDJPY=X",
            decision_type="bogus_type", decision="hold",
        )


def test_get_freshness_returns_latest_for_snapshot(store: OrchestratorStore) -> None:
    """同一 snapshot に複数 freshness 行があれば最新 (id 最大) を返す。"""
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    store.record_freshness(snapshot_id=snap_id, pair="USDJPY=X", price_age_sec=10.0)
    store.record_freshness(snapshot_id=snap_id, pair="USDJPY=X", price_age_sec=99.0)
    fresh = store.get_freshness_for_snapshot(snap_id)
    assert fresh.price_age_sec == 99.0   # 後に入れた行 (最新) が返る


def test_record_vote_reflected_true_roundtrips_to_bool(store: OrchestratorStore) -> None:
    """reflected_in_plan=True が int 1 で保存され bool True に正規化されて返る。"""
    snap_id = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X", snapshot_id=snap_id)
    decision_id = store.record_decision(
        run_id=run_id, snapshot_id=snap_id, pair="USDJPY=X",
        decision_type="direct_hold", decision="hold",
    )
    store.record_vote(
        decision_id=decision_id, agent_run_id=run_id, agent_name="NewsAgent",
        vote_action="buy", reflected_in_plan=True,
    )
    votes = store.get_votes(decision_id)
    assert len(votes) == 1
    assert votes[0].reflected_in_plan is True


# ── Task 2.1: agent_outputs / execution_opinions / supersede ──────────


def test_record_and_get_agent_outputs(store: OrchestratorStore) -> None:
    run_id = store.start_run(
        "PlannerAgent", pair="USDJPY=X", trigger_type="planning_cycle"
    )
    oid = store.record_agent_output(
        run_id=run_id, agent_name="PlannerAgent", pair="USDJPY=X",
        output_type="opportunity", action="buy", score=0.7, confidence=0.6,
        reasoning_summary="test", structured_payload={"opportunity": True},
    )
    assert isinstance(oid, int) and oid > 0
    rows = store.get_agent_outputs(run_id)
    assert len(rows) == 1
    assert rows[0].agent_name == "PlannerAgent"
    assert rows[0].action == "buy"
    assert rows[0].structured_payload_json == {"opportunity": True}
    assert rows[0].saved_at is not None


def test_get_agent_outputs_id_ascending(store: OrchestratorStore) -> None:
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    first = store.record_agent_output(run_id=run_id, agent_name="TechnicalAgent")
    second = store.record_agent_output(run_id=run_id, agent_name="NewsAgent")
    rows = store.get_agent_outputs(run_id)
    assert [r.id for r in rows] == [first, second]


def test_record_and_get_execution_opinion(store: OrchestratorStore) -> None:
    run_id = store.start_run("ExecutionOpinionAgent", pair="USDJPY=X")
    eid = store.record_execution_opinion(
        run_id=run_id, pair="USDJPY=X", action="buy",
        entry_reference_price=150.0, sl=149.5, tp=151.0, rr=2.0,
        reasoning_summary="draft",
    )
    assert isinstance(eid, int) and eid > 0
    op = store.get_execution_opinion(run_id)
    assert op is not None
    assert op.pair == "USDJPY=X"
    assert op.rr == 2.0
    # latest = highest id
    store.record_execution_opinion(run_id=run_id, pair="USDJPY=X", action="sell", rr=1.5)
    op2 = store.get_execution_opinion(run_id)
    assert op2.action == "sell"


def test_get_execution_opinion_none_when_absent(store: OrchestratorStore) -> None:
    assert store.get_execution_opinion(999) is None


def test_supersede_active_plans(store: OrchestratorStore) -> None:
    snap = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    p1 = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    p2 = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    # keep p2, supersede the rest
    superseded = store.supersede_active_plans("USDJPY=X", except_plan_id=p2)
    assert p1 in superseded
    assert p2 not in superseded
    active = store.get_active_plans("USDJPY=X")
    active_ids = {pl.plan_id for pl in active}
    assert p2 in active_ids
    assert p1 not in active_ids


def test_supersede_active_plans_scopes_to_pair(store: OrchestratorStore) -> None:
    """supersede は対象 pair の active plan のみを変更し他 pair には触れない。"""
    snap = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    usd = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    eur = store.create_trade_plan(
        pair="EURUSD=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    superseded = store.supersede_active_plans("USDJPY=X")
    assert usd in superseded
    assert eur not in superseded
    assert eur in {pl.plan_id for pl in store.get_active_plans("EURUSD=X")}


def test_supersede_only_touches_active_plans(store: OrchestratorStore) -> None:
    """並走 watch loop が active でなくした plan を supersede が上書きしない (High)。

    supersede は status='active' の plan だけを変更する。get_active_plans の読み取りと
    update の間に triggered/invalidated へ遷移した plan は対象外でなければ trigger 記録や
    lifecycle metric を壊す。status 条件付きの単一 UPDATE でこれを保証する。
    """
    snap = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    p1 = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    p2 = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    # watch loop が p1 を triggered にした (active でなくなった) と仮定
    store.update_plan_status(p1, "triggered")
    superseded = store.supersede_active_plans("USDJPY=X")
    # p1 は active でないので superseded にならず、triggered のまま残る
    assert p1 not in superseded
    assert store.get_trade_plan(p1).status == "triggered"
    # p2 は active だったので superseded になる
    assert p2 in superseded
    assert store.get_trade_plan(p2).status == "superseded"


def test_supersede_does_not_clobber_stale_read(store: OrchestratorStore) -> None:
    """get_active_plans が返した後に triggered へ遷移した plan を上書きしない (High TOCTOU)。

    旧実装 (read → 各 plan を無条件 update_plan_status) では、stale read に含まれた
    plan を triggered のまま上書きしてしまう。status 条件付き単一 UPDATE なら、
    UPDATE 時点で active でない plan は対象外になる。
    get_active_plans を「triggered 済 plan を stale に含めて返す」よう差し替えて検証する。
    """
    snap = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    p1 = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    stale_snapshot = store.get_active_plans("USDJPY=X")  # p1 を active として読む
    # 読み取り後に watch loop が p1 を triggered にした
    store.update_plan_status(p1, "triggered")
    # supersede が万一 stale な read を信じて update_plan_status を回しても triggered を
    # 壊さないこと。get_active_plans を stale 読みに差し替えて最悪条件を作る。
    store.get_active_plans = lambda pair=None: stale_snapshot  # type: ignore[assignment]
    superseded = store.supersede_active_plans("USDJPY=X")
    assert p1 not in superseded
    assert store.get_trade_plan(p1).status == "triggered"  # 上書きされない


def test_record_agent_output_serializes_datetime_payload(store: OrchestratorStore) -> None:
    """structured_payload に datetime が含まれても保存できる (Medium)。

    ExecutionPlanDraft.expires_at: datetime のような値を含む payload を渡しても
    'Object of type datetime is not JSON serializable' で落ちず、読み戻せる
    (datetime は ISO 文字列に正規化される)。
    """
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    expires = datetime(2026, 6, 27, 12, 0, 0)
    oid = store.record_agent_output(
        run_id=run_id,
        agent_name="ExecutionOpinionAgent",
        pair="USDJPY=X",
        structured_payload={"expires_at": expires, "direction": "long", "rr": 2.0},
    )
    assert isinstance(oid, int) and oid > 0
    rows = store.get_agent_outputs(run_id)
    assert len(rows) == 1
    payload = rows[0].structured_payload_json
    # datetime は ISO 文字列として保存される (JSON 互換)
    assert payload["expires_at"] == expires.isoformat()
    assert payload["direction"] == "long"
    assert payload["rr"] == 2.0


# ── shadow_triggers (§8.1) ─────────────────────────────────────


def test_record_and_get_shadow_trigger(store: OrchestratorStore) -> None:
    """watch loop の plan_trigger を shadow_triggers に保存し plan_id で読み戻せる。"""
    snap = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, 12, 0, 0),
    )
    run_id = store.start_run("OrchestratorRuntime", pair="USDJPY=X")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    decision_id = store.record_decision(
        run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
        decision_type="plan_trigger", plan_id=plan_id,
    )
    tid = store.record_shadow_trigger(
        plan_id=plan_id,
        decision_id=decision_id,
        pair="USDJPY=X",
        direction="long",
        triggered_at=datetime(2026, 6, 21, 9, 0, 0),
        trigger_price=150.12,
        sl=149.4,
        tp=151.5,
        rr=2.0,
        snapshot_id=snap,
        risk_gate_result={"passed": True, "reject_class": None, "issues": []},
    )
    assert isinstance(tid, int) and tid > 0

    trig = store.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.plan_id == plan_id
    assert trig.decision_id == decision_id
    assert trig.pair == "USDJPY=X"
    assert trig.direction == "long"
    assert trig.triggered_at == datetime(2026, 6, 21, 9, 0, 0)
    assert trig.trigger_price == 150.12
    assert trig.rr == 2.0
    assert trig.snapshot_id == snap
    assert trig.risk_gate_result_json["passed"] is True


def test_get_shadow_trigger_none_when_absent(store: OrchestratorStore) -> None:
    assert store.get_shadow_trigger(999) is None


def test_shadow_trigger_plan_id_is_unique(store: OrchestratorStore) -> None:
    """同一 plan_id への 2 回目の shadow_trigger は UNIQUE 制約で拒否される。

    並行 watch が原子 claim を擦り抜けても、shadow_triggers 重複は DB が防ぐ
    (defense-in-depth, Codex High#1)。
    """
    from sqlalchemy.exc import IntegrityError

    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 6, 21, 9, 0, 0))
    run_id = store.start_run("OrchestratorRuntime", pair="USDJPY=X")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )
    store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=datetime(2026, 6, 21, 9, 0, 0),
    )
    with pytest.raises(IntegrityError):
        store.record_shadow_trigger(
            plan_id=plan_id, decision_id=None, pair="USDJPY=X", direction="long",
            triggered_at=datetime(2026, 6, 21, 9, 1, 0),
        )


# ── try_claim_plan_status (条件付き状態遷移, Codex High#1/#2) ───


@pytest.fixture
def active_plan(store: OrchestratorStore) -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 6, 21, 9, 0, 0))
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    return store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 6, 27, 12, 0, 0), created_by_run_id=run_id,
    )


def test_try_claim_plan_status_succeeds_from_active(
    store: OrchestratorStore, active_plan: int
) -> None:
    assert store.try_claim_plan_status(active_plan, "triggered") is True
    assert store.get_trade_plan(active_plan).status == "triggered"


def test_try_claim_plan_status_fails_when_not_active(
    store: OrchestratorStore, active_plan: int
) -> None:
    """別経路で triggered になった plan を古い watch 評価が invalidated に戻せない。"""
    assert store.try_claim_plan_status(active_plan, "triggered") is True
    # 既に triggered: 2 回目の active→invalidated claim は負ける
    assert store.try_claim_plan_status(active_plan, "invalidated") is False
    assert store.get_trade_plan(active_plan).status == "triggered"  # 上書きされない


def test_try_mark_plan_triggered_is_conditional_claim(
    store: OrchestratorStore, active_plan: int
) -> None:
    """try_mark_plan_triggered は active のときだけ勝ち、2 回目は負ける。"""
    assert store.try_mark_plan_triggered(active_plan) is True
    assert store.try_mark_plan_triggered(active_plan) is False
    assert store.get_trade_plan(active_plan).status == "triggered"


# ── shadow_hindsight_evaluations (§8.1 / Phase 4) ──────────────


def _shadow_trigger(
    store: OrchestratorStore, *, triggered_at: datetime, trigger_price: float = 150.0,
    sl: float = 149.0, tp: float = 152.0, direction: str = "long",
) -> int:
    """テスト用に shadow_trigger を 1 件作って id を返す。"""
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=triggered_at)
    run_id = store.start_run("OrchestratorRuntime", pair="USDJPY=X")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction=direction,
        entry_conditions_json=[], action_json={"sl": sl, "tp": tp, "rr": 2.0},
        invalidation_json=[], expires_at=triggered_at, created_by_run_id=run_id,
    )
    return store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair="USDJPY=X", direction=direction,
        triggered_at=triggered_at, trigger_price=trigger_price, sl=sl, tp=tp, rr=2.0,
        snapshot_id=snap,
    )


def test_record_hindsight_evaluation_pending(store: OrchestratorStore) -> None:
    """trigger 時に status=pending の hindsight 行を作り読み戻せる (Plan C enqueue)。"""
    trig_id = _shadow_trigger(store, triggered_at=datetime(2026, 6, 21, 9, 0, 0))
    hid = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    assert isinstance(hid, int) and hid > 0

    ev = store.get_hindsight_evaluation(trig_id)
    assert ev is not None
    assert ev.shadow_trigger_id == trig_id
    assert ev.status == "pending"
    assert ev.horizon_seconds == 86400
    assert ev.mfe_r is None
    assert ev.evaluated_at is None


def test_get_hindsight_evaluation_none_when_absent(store: OrchestratorStore) -> None:
    assert store.get_hindsight_evaluation(999) is None


def test_record_hindsight_evaluation_duplicate_is_noop(store: OrchestratorStore) -> None:
    """同一 shadow_trigger への 2 回目 enqueue は UNIQUE で弾くが crash させず既存 id を返す。

    trigger 記録パスの一部なので、重複時に例外を投げると trigger 記録ごと巻き戻る。
    冪等に既存 id を返す (record_shadow_trigger と同思想の IntegrityError ハンドリング)。
    """
    trig_id = _shadow_trigger(store, triggered_at=datetime(2026, 6, 21, 9, 0, 0))
    first = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    second = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    assert second == first  # 既存行の id を返す (新規作成しない)
    # 行は 1 件のまま
    assert store.get_hindsight_evaluation(trig_id).id == first


def test_update_hindsight_evaluation_fills_metrics(store: OrchestratorStore) -> None:
    """poll loop が pending 行に metric を埋め status=evaluated にする。"""
    trig_id = _shadow_trigger(store, triggered_at=datetime(2026, 6, 21, 9, 0, 0))
    hid = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    store.update_hindsight_evaluation(
        hid,
        status="evaluated",
        evaluated_at=datetime(2026, 6, 22, 9, 0, 0),
        mfe_r=1.5, mae_r=-0.4, pnl_r=1.5,
        would_hit_sl=False, would_hit_tp=True,
        reasoning_summary="hit TP within 24h",
    )
    ev = store.get_hindsight_evaluation(trig_id)
    assert ev.status == "evaluated"
    assert ev.evaluated_at == datetime(2026, 6, 22, 9, 0, 0)
    assert ev.mfe_r == 1.5
    assert ev.mae_r == -0.4
    assert ev.pnl_r == 1.5
    assert ev.would_hit_sl is False
    assert ev.would_hit_tp is True
    assert ev.reasoning_summary == "hit TP within 24h"


def test_update_hindsight_evaluation_can_mark_failed(store: OrchestratorStore) -> None:
    """価格データ不足等で評価不能なら status=failed に倒せる。"""
    trig_id = _shadow_trigger(store, triggered_at=datetime(2026, 6, 21, 9, 0, 0))
    hid = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    store.update_hindsight_evaluation(
        hid, status="failed", evaluated_at=datetime(2026, 6, 22, 9, 0, 0),
        reasoning_summary="no ohlcv in horizon",
    )
    ev = store.get_hindsight_evaluation(trig_id)
    assert ev.status == "failed"
    assert ev.mfe_r is None


def test_get_pending_hindsight_evaluations_respects_horizon(
    store: OrchestratorStore,
) -> None:
    """pending 行のうち triggered_at + horizon を過ぎたものだけ poll が拾う。"""
    # 25h 前に trigger → horizon 86400(24h) 経過済み = 評価対象
    ready = _shadow_trigger(store, triggered_at=datetime(2026, 6, 20, 8, 0, 0))
    store.record_hindsight_evaluation(shadow_trigger_id=ready, horizon_seconds=86400)
    # 1h 前に trigger → horizon 未経過 = 対象外
    notyet = _shadow_trigger(store, triggered_at=datetime(2026, 6, 21, 8, 0, 0))
    store.record_hindsight_evaluation(shadow_trigger_id=notyet, horizon_seconds=86400)

    now = datetime(2026, 6, 21, 9, 0, 0)
    pending = store.get_pending_hindsight_evaluations(now=now)
    ids = {p.shadow_trigger_id for p in pending}
    assert ready in ids
    assert notyet not in ids


def test_get_pending_hindsight_excludes_already_evaluated(
    store: OrchestratorStore,
) -> None:
    """evaluated/failed 済みの行は poll が再度拾わない。"""
    trig_id = _shadow_trigger(store, triggered_at=datetime(2026, 6, 20, 8, 0, 0))
    hid = store.record_hindsight_evaluation(
        shadow_trigger_id=trig_id, horizon_seconds=86400,
    )
    store.update_hindsight_evaluation(
        hid, status="evaluated", evaluated_at=datetime(2026, 6, 21, 9, 0, 0),
        mfe_r=1.0, mae_r=0.0, pnl_r=1.0, would_hit_sl=False, would_hit_tp=True,
    )
    pending = store.get_pending_hindsight_evaluations(
        now=datetime(2026, 6, 21, 9, 0, 0)
    )
    assert all(p.shadow_trigger_id != trig_id for p in pending)


# ── P-5: decision_snapshots に position_json / current_plan_json ──


def test_snapshot_persists_position_and_current_plan(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "o.db")
    pos = {"count": 1, "items": [{"direction": "long", "entry_price": 150.0}]}
    cur = {"plan_id": 9, "status": "active"}
    sid = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 12, 0),
        position_json=pos, current_plan_json=cur,
    )
    snap = store.get_snapshot(sid)
    assert snap.position_json == pos
    assert snap.current_plan_json == cur


def test_snapshot_migration_adds_columns(tmp_path: Path) -> None:
    """旧 schema (position_json 無し) の DB を開くと ALTER で列が生える。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE decision_snapshots ("
        "snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, pair VARCHAR NOT NULL,"
        "as_of_time DATETIME NOT NULL, quote_json JSON, technical_ref JSON,"
        "news_ref JSON, created_at DATETIME NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = OrchestratorStore(db)
    sid = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 12, 0),
        position_json={"count": 0, "items": []},
    )
    assert store.get_snapshot(sid).position_json == {"count": 0, "items": []}


# ── P-2b: trade_plans に scale_in / new_signal_evidence ──


def test_trade_plan_persists_scale_in_fields(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "o.db")
    pid = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=1, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.0}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
        scale_in=True, new_signal_evidence="new 1h breakout",
    )
    plan = store.get_trade_plan(pid)
    assert plan.scale_in is True
    assert plan.new_signal_evidence == "new 1h breakout"


def test_trade_plan_scale_in_defaults_null(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "o.db")
    pid = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=1, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.0}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    plan = store.get_trade_plan(pid)
    assert plan.scale_in is None
    assert plan.new_signal_evidence is None


def test_trade_plan_migration_adds_scale_in_columns(tmp_path: Path) -> None:
    """旧 schema (scale_in 無し) の DB を開くと ALTER で列が生える。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trade_plans ("
        "plan_id INTEGER PRIMARY KEY AUTOINCREMENT, pair VARCHAR NOT NULL,"
        "snapshot_id INTEGER, horizon VARCHAR, direction VARCHAR,"
        "entry_conditions_json JSON, action_json JSON, invalidation_json JSON,"
        "expires_at DATETIME, status VARCHAR NOT NULL,"
        "created_by_run_id INTEGER, created_at DATETIME NOT NULL,"
        "updated_at DATETIME NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = OrchestratorStore(db)
    pid = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=1, horizon="day", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
        scale_in=True, new_signal_evidence="new 1h breakout",
    )
    plan = store.get_trade_plan(pid)
    assert plan.scale_in is True
    assert plan.new_signal_evidence == "new 1h breakout"


# ── Task 1: get_plans_by_status (取得層) ───────────────────────


def _seed_plan(store, *, pair, status, direction="long", created_offset_sec=0):
    """任意 status の plan を ORM 直挿しで seed する (pending_approval も可能)。

    create_trade_plan() は PLAN_STATUSES 外の status を拒否するため、テストでは
    ORM で直接 insert する。取得層は status 文字列一致で引くだけなので DB に行が
    あれば読める。created_at をずらして降順ソートを検証可能にする。
    """
    from datetime import timedelta
    from src.utils.clock import db_now
    now = db_now() + timedelta(seconds=created_offset_sec)
    with Session(store._engine) as s:
        plan = _TradePlan(
            pair=pair, snapshot_id=1, horizon="day", direction=direction,
            entry_conditions_json=[], action_json={"sl": 1.0, "tp": 2.0},
            invalidation_json=[], expires_at=now + timedelta(hours=8),
            status=status, created_by_run_id=1,
            created_at=now, updated_at=now,
        )
        s.add(plan)
        s.commit()
        return plan.plan_id


def test_get_plans_by_status_active_only(store: OrchestratorStore) -> None:
    a = _seed_plan(store, pair="USDJPY=X", status="active")
    _seed_plan(store, pair="USDJPY=X", status="suspended")
    rows = store.get_plans_by_status(("active",))
    ids = {p.plan_id for p in rows}
    assert a in ids
    assert all(p.status == "active" for p in rows)


def test_get_plans_by_status_pending_approval(store: OrchestratorStore) -> None:
    p = _seed_plan(store, pair="USDJPY=X", status="pending_approval")
    _seed_plan(store, pair="USDJPY=X", status="active")
    rows = store.get_plans_by_status(("pending_approval",))
    ids = {r.plan_id for r in rows}
    assert ids == {p}


def test_get_plans_by_status_empty_tuple_returns_empty(store: OrchestratorStore) -> None:
    _seed_plan(store, pair="USDJPY=X", status="active")
    assert store.get_plans_by_status(()) == []


def test_get_plans_by_status_orders_created_desc(store: OrchestratorStore) -> None:
    older = _seed_plan(store, pair="USDJPY=X", status="active", created_offset_sec=0)
    newer = _seed_plan(store, pair="USDJPY=X", status="active", created_offset_sec=10)
    rows = store.get_plans_by_status(("active",))
    assert [r.plan_id for r in rows[:2]] == [newer, older]


def test_get_plans_by_status_pair_filter(store: OrchestratorStore) -> None:
    usd = _seed_plan(store, pair="USDJPY=X", status="active")
    _seed_plan(store, pair="EURUSD=X", status="active")
    rows = store.get_plans_by_status(("active",), pair="USDJPY=X")
    assert {r.plan_id for r in rows} == {usd}
