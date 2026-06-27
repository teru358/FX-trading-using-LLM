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


def test_sizing_failure_before_submit_abandons_and_invalidates(tmp_path):
    """code review M1/M2 + codex High: signal 構築 (sizing) の失敗は submit マーキングより
    前なので broker は呼ばれず未発注。phase 追跡の except が intent=abandoned + plan=
    invalidated を即時記録する (daemon 生存中も triggered のまま放置しない)。pip_value=0 で
    ZeroDivision を誘発する。"""
    from src.config.schema import AppConfig, InstrumentConfig, TradingConfig

    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    # pip_value=0 + 大きな balance で _calculate_position_size を ZeroDivision に倒す。
    rt._app_config = AppConfig(
        trading=TradingConfig(risk_per_trade=0.02, min_lot_size=1000.0, lot_unit=1000.0),
        instruments=[InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", pip_value=0.0)],
    )
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                         # broker 未送信
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "abandoned"               # submit 前失敗 → 未発注として terminal 化
    assert intent.submitted_at is None                # 復旧分岐点に到達していない
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"  # 永久ブロック回避


class _RaisingBroker:
    """execute_signal で例外を投げる (送信後フェーズのクラッシュを模擬)。"""

    def __init__(self):
        self.calls = []

    def execute_signal(self, signal, position_mgr, macro_context=""):
        self.calls.append(signal)
        raise RuntimeError("broker connection lost mid-send")


def test_broker_exception_after_submit_marks_needs_reconcile(tmp_path):
    """codex High: broker.execute_signal が例外を投げる = 送信後・応答前のクラッシュ。
    建玉不明なので needs_reconcile で隔離し、plan は triggered のまま (terminal 化しない)。"""
    broker = _RaisingBroker()
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert len(broker.calls) == 1                      # 送信は試みた
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.recovery_status == "needs_reconcile"  # 隔離
    assert intent.status == "submitted"                # 触らない (建玉あるかもしれない)
    assert intent.submitted_at is not None             # submit マーク済
    assert rt._orch.get_trade_plan(plan_id).status == "triggered"  # terminal 化しない


def test_trigger_id_recorded_on_order_intent(tmp_path):
    """codex Medium: shadow_trigger_id が order_intent.trigger_id に記録され trace が繋がる。"""
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = make_live_runtime(tmp_path, broker, _GatePass())
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    intent = rt._orch.get_order_intent(plan_id)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert intent.trigger_id == str(trig.id)


def test_shadow_mode_never_executes(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    # mode="shadow": execution_broker を渡しても live 分岐に入らない
    rt = make_live_runtime(tmp_path, broker, _GatePass(), mode="shadow")
    seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                         # shadow 境界維持
