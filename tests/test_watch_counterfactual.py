"""反実仮想 watch (gate spec F-4) のテスト。

評価意味論は active と同一・action 境界のみ違う:
- pending: entry 初成立 → stamp のみ (shadow 行なし・plan は pending のまま承認可能)
- pending: expiry/invalidation → unanswered 終端 (+stamp 済なら cf finalize)
- rejected: entry → cf 行を直接 finalize / invalidation → latch
- 執行境界: live mode + broker 注入でも cf 経路は執行しない
fixture は tests/test_watch_loop_shadow.py のパターン (db_now() 相対) を踏襲。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.price_analyzer import PriceAnalysis
from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime
from src.utils.clock import db_now

NOW = db_now()
FUTURE = NOW + timedelta(hours=8)
PAST = NOW - timedelta(hours=1)


def _seed_ok_technical(db: Path) -> None:
    """freshness final wall を通す ok technical snapshot (age=60s で fresh)。"""
    AnalysisStore(db).add_snapshot(
        PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.7,
            entry_zone=(149.0, 151.0), reasoning_summary="seed",
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def _make_runtime(
    tmp_path: Path, *, mid: float, spread: float = 0.01,
    seed_technical: bool = False, mode: str = "shadow", execution_broker=None,
) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    if seed_technical:
        _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - spread / 2, ask=mid + spread / 2, mid=mid, spread=spread,
            source="test", observed_at=NOW,
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        mode=mode,
        execution_broker=execution_broker,
    )


def _create_gate_plan(
    orch: OrchestratorStore, *, status: str = "pending_approval",
    entry: list[dict] | None = None, invalidation: list[dict] | None = None,
    expires_at: datetime = FUTURE,
) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=entry or [{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=invalidation or [],
        expires_at=expires_at, created_by_run_id=run_id, status=status,
    )


# ── Task 9: pending の entry stamp ────────────────────────────


def test_pending_entry_stamps_without_shadow_row(tmp_path):
    """entry 成立 → stamp のみ。shadow 行なし・status は pending のまま (承認可能)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []                       # real trigger には数えない
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "pending_approval"     # 承認可能なまま
    assert plan.cf_state == "would_trigger"
    assert plan.cf_stamp_price == 150.10
    assert plan.cf_stamped_at is not None
    assert rt._orch.get_shadow_trigger(plan_id) is None  # 行は書かない (UNIQUE 保護)


def test_pending_entry_not_hold_no_stamp(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.50)  # 閾値の上 — 未成立
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state is None


def test_pending_stamp_only_once(tmp_path):
    """2 tick 目は stamp を上書きしない (最初の成立瞬間がエントリー点)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    first = rt._orch.get_trade_plan(plan_id).cf_stamped_at
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))
    assert rt._orch.get_trade_plan(plan_id).cf_stamped_at == first


def test_pending_freshness_wall_blocks_stamp(tmp_path):
    """freshness 失敗時は stamp しない (active と同じ物差し)。technical seed なし。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=False)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state is None


def test_stamped_pending_approved_then_real_trigger_no_conflict(tmp_path):
    """stamp 済み pending を承認 → active → real trigger が UNIQUE 衝突なく記録される。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)                       # stamp
    assert rt._orch.try_decide_gate(plan_id, "approved") is True
    triggered = rt.run_watch_cycle(now=NOW + timedelta(seconds=2))  # real trigger
    assert triggered == [plan_id]
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "triggered"
    assert plan.cf_state == "would_trigger"           # stamp は残る (承認遅延コスト素材)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None                           # real 行 1 本のみ


# ── Task 10: pending の unanswered 終端 ───────────────────────


def test_pending_expiry_marks_unanswered_no_stamp(tmp_path):
    """stamp なしで TTL 到達 → expired + unanswered、cf 行なし。"""
    rt = _make_runtime(tmp_path, mid=150.50)  # entry 未成立の価格
    plan_id = _create_gate_plan(rt._orch, expires_at=PAST)
    rt.run_watch_cycle(now=NOW)
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_pending_expiry_with_stamp_finalizes_cf(tmp_path):
    """stamp → TTL 到達 → expired + unanswered + cf 行 (triggered_at=stamp 時刻)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch, expires_at=NOW + timedelta(seconds=30))
    rt.run_watch_cycle(now=NOW)                          # stamp
    stamped_at = rt._orch.get_trade_plan(plan_id).cf_stamped_at
    rt.run_watch_cycle(now=NOW + timedelta(minutes=5))   # TTL 超過
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.triggered_at == stamped_at               # エントリー点は stamp 時刻
    assert trig.trigger_price == 150.10


def test_pending_invalidation_marks_unanswered(tmp_path):
    """invalidation 成立 → invalidated + unanswered (G-3 拡張解釈)。承認不能になる。"""
    rt = _make_runtime(tmp_path, mid=147.50)  # invalidation (price_below 148) が成立
    plan_id = _create_gate_plan(
        rt._orch,
        entry=[{"type": "price_at_or_below", "value": 147.0}],  # entry は未成立
        invalidation=[{"type": "price_below", "value": 148.0}],
    )
    rt.run_watch_cycle(now=NOW)
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "invalidated"
    assert plan.gate_decision == "unanswered"
    # 承認不能 (409 相当)
    assert rt._orch.try_decide_gate(plan_id, "approved") is False


# ── Task 11: rejected の cf finalize / latch / 執行境界 ────────


def _rejected_plan(rt, **kw) -> int:
    plan_id = _create_gate_plan(rt._orch, **kw)
    assert rt._orch.try_decide_gate(plan_id, "rejected") is True
    return plan_id


def test_rejected_entry_finalizes_cf_directly(tmp_path):
    """reject 後の entry 初成立 → cf 行 (status は rejected のまま不変)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _rejected_plan(rt)
    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == []                     # real trigger には数えない
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "rejected"           # terminal のまま
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.trigger_price == 150.10
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_cf_trigger"   # real と集計分離


def test_rejected_cf_recorded_once(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _rejected_plan(rt)
    rt.run_watch_cycle(now=NOW)
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))  # 2 tick 目
    # 行は 1 本のまま (UNIQUE + cf_state 解決済みで watch 対象から外れる)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None


def test_rejected_invalidation_latches_then_entry_ignored(tmp_path):
    """invalidation latch 後は entry が成立しても cf 行を書かない (誤反実仮想の防止)。

    entry (price_at_or_below 149.0) と invalidation (price_below 148.0) は
    非重複な閾値にしてある: tick2 の価格 148.5 は invalidation を再発火させず
    (148.5 は 148.0 未満ではない) entry のみ成立させる。これにより「tick2 で
    invalidation が再発火して entry 評価に到達しない」経路を排除し、
    cf_state='invalidated' の latch そのものが cf 行書き込みを防いでいることを
    ピンポイントで検証する。
    """
    # tick1: mid=147.5 → invalidation (price_below 148) 成立 → latch
    rt = _make_runtime(tmp_path, mid=147.50, seed_technical=True)
    plan_id = _rejected_plan(
        rt,
        entry=[{"type": "price_at_or_below", "value": 149.0}],
        invalidation=[{"type": "price_below", "value": 148.0}],
    )
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state == "invalidated"
    # tick2: 148.5 は invalidation 非該当 (148.5 は 148.0 未満ではない) だが
    # entry (≤149.0) は成立圏。latch がなければここで cf 行が書かれてしまう。
    rt2_quote_mid = 148.50
    rt._quote_provider = lambda pair: QuoteSnapshot(
        bid=rt2_quote_mid - 0.005, ask=rt2_quote_mid + 0.005, mid=rt2_quote_mid,
        spread=0.01, source="test", observed_at=NOW,
    )
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_cf_paths_never_execute_in_live_mode(tmp_path):
    """live mode + broker 注入でも cf 経路は執行しない (spec F-4 執行境界)。"""
    broker = MagicMock()
    rt = _make_runtime(
        tmp_path, mid=150.10, seed_technical=True,
        mode="live", execution_broker=broker,
    )
    _rejected_plan(rt)
    pending_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    # rejected は cf 行化・pending は stamp、どちらも broker に触れない
    broker.assert_not_called()
    assert not broker.method_calls
    assert rt._orch.get_order_intent(pending_id) is None


# ── Task 12: finalize 待ち回収 (crash recovery) ────────────────


def test_crashed_rejected_finalize_recovered_next_tick(tmp_path):
    """reject 直後 (finalize 前) にクラッシュした状態 → 次 tick で cf 行が揃う。"""
    rt = _make_runtime(tmp_path, mid=150.50)  # entry 非成立の価格 (回収経路のみを検証)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_decide_gate(plan_id, "rejected")
    # ここで crash した想定: rejected + would_trigger + 行なし
    assert rt._orch.get_shadow_trigger(plan_id) is None

    rt.run_watch_cycle(now=NOW)

    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.trigger_price == 150.25        # stamp 値で記録される
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_cf_trigger"


def test_crashed_expired_finalize_recovered(tmp_path):
    """expiry 遷移後 (finalize 前) のクラッシュも回収される。"""
    rt = _make_runtime(tmp_path, mid=150.50)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_close_pending_unanswered(plan_id, "expired")
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state == "triggered"
    assert rt._orch.get_shadow_trigger(plan_id) is not None


def test_recovery_idempotent_across_ticks(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.50)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_decide_gate(plan_id, "rejected")
    rt.run_watch_cycle(now=NOW)
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))  # 2 周しても行は増えない
    assert rt._orch.get_shadow_trigger(plan_id) is not None
    assert rt._orch.get_cf_finalize_pending("USDJPY=X") == []


def test_approved_after_exec_failure_not_cf_recovered(tmp_path):
    """stamp→approve→real trigger→発注失敗で invalidated → cf 復旧されない
    (codex plan review High#1 の end-to-end 回帰)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)                          # stamp
    assert rt._orch.try_decide_gate(plan_id, "approved") is True
    rt.run_watch_cycle(now=NOW + timedelta(seconds=2))   # real trigger
    # live 発注失敗経路の simulate (runtime は is_executed=False で invalidated 化)
    rt._orch.update_plan_status(plan_id, "invalidated")
    rt.run_watch_cycle(now=NOW + timedelta(seconds=4))   # 回収 tick
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.cf_state == "would_trigger"              # cf 化されない (状態不変)
    trig = rt._orch.get_shadow_trigger(plan_id)          # 行は real の 1 本のまま
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_trigger"           # plan_cf_trigger が増えていない


def test_approved_invalidated_before_real_trigger_not_cf_recovered(tmp_path):
    """real trigger 前 (shadow_trigger 行なし) に承認済み plan が invalidated 化しても
    cf 復旧されない (codex plan review High#1)。

    test_approved_after_exec_failure_not_cf_recovered は real trigger 後に発注失敗を
    simulate するため、既存 shadow_trigger 行の UNIQUE(plan_id) 制約が cf 行の
    偽 INSERT を独立にブロックしてしまい、get_cf_finalize_pending の
    gate_decision IN ('rejected','unanswered') ガードを外しても検知できない。
    本テストは real trigger を一切起こさず (shadow_trigger 行なし) に
    invalidated 化するため、UNIQUE 制約による保護がなく、ガードのみが
    唯一の防波堤になる。
    """
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)                          # stamp (cf_state='would_trigger')
    assert rt._orch.try_decide_gate(plan_id, "approved") is True
    # real trigger は起こさない — shadow_trigger 行は存在しない状態を維持する。
    assert rt._orch.get_shadow_trigger(plan_id) is None
    # 発注失敗等で invalidated 化する経路を simulate (real trigger を経ていない点が
    # test_approved_after_exec_failure_not_cf_recovered との違い)。
    rt._orch.update_plan_status(plan_id, "invalidated")
    rt.run_watch_cycle(now=NOW + timedelta(seconds=4))   # 回収 tick
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.cf_state == "would_trigger"               # triggered に進んでいない
    assert rt._orch.get_shadow_trigger(plan_id) is None    # cf 行が書かれていない
