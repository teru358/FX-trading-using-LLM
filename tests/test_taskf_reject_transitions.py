"""Task F (codex #4): reject/failed/halted 後の plan/intent 遷移。永久ブロック回避。"""
from __future__ import annotations

from src.trading.broker_adapter import ExecutionResult

from tests.test_taskf_live_execution_helpers import (
    NOW,
    _FakeBroker,
    _GatePass,
    _GateReject,
    make_live_runtime,
    seed_active_plan_ready_to_trigger,
)


def test_structural_reject_invalidates_plan_and_keeps_rejected(tmp_path):
    """恒久 reject: plan=invalidated, intent=rejected。再 trigger されない。"""
    rt = make_live_runtime(tmp_path, _FakeBroker(None), _GateReject("structural"))
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_order_intent(plan_id).status == "rejected"
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "invalidated"
    # 再度 watch しても active でないので再評価されない (同一 plan は蘇生しない)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_order_intent(plan_id).status == "rejected"  # 変化なし


def test_fixable_reject_abandons_intent_and_invalidates_plan(tmp_path):
    """一時 reject: intent=abandoned (provenance 区別), plan=invalidated。再発注は新 plan で。"""
    rt = make_live_runtime(tmp_path, _FakeBroker(None), _GateReject("fixable"))
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_order_intent(plan_id).status == "abandoned"
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"


def test_failed_execution_invalidates_plan(tmp_path):
    """broker failed (技術失敗) → intent=failed, plan=invalidated。再発注は新 plan で。"""
    rt = make_live_runtime(
        tmp_path, _FakeBroker(ExecutionResult.failed("bridge down")), _GatePass(),
    )
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_order_intent(plan_id).status == "failed"
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"
