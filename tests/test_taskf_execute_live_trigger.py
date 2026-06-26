"""Task F F-1/F-3: _execute_live_trigger 執行段テスト (claim→gate→submit→execute→反映)。"""
from __future__ import annotations

from src.trading.broker_adapter import ExecutionResult
from src.utils.clock import db_now

from tests.test_taskf_live_execution_helpers import (
    NOW,
    _FakeBroker,
    _GatePass,
    _GateReject,
    _executed_order,
    make_live_runtime,
    seed_active_plan_ready_to_trigger,
)


def test_live_trigger_executes_and_records_filled(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    plan_id = seed_active_plan_ready_to_trigger(rt)
    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == [plan_id]
    assert len(broker.calls) == 1                    # 発注 1 回
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "filled"
    assert intent.order_id is not None


def test_live_trigger_structural_reject_does_not_execute(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GateReject("structural"))
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                         # gate reject → 発注なし
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "rejected"


def test_live_trigger_duplicate_intent_aborts(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    plan_id = seed_active_plan_ready_to_trigger(rt)
    # 既に同 plan_id の intent が存在 (前回発注) → UNIQUE で中止
    rt._orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=99, lease_until=db_now(),
    )
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                         # 既発注 → 二重発注しない


def test_shadow_mode_never_executes(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    # mode="shadow": execution_broker を渡しても live 分岐に入らない
    rt = make_live_runtime(tmp_path, broker, _GatePass(), mode="shadow")
    seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                         # shadow 境界維持
