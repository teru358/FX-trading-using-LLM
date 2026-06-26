"""Task F live 執行段テストの共有 helper (test_taskf_execute_live_trigger /
test_taskf_reject_transitions が import する)。

既存 tests/test_watch_loop_shadow.py の seed パターン (db_now 相対の NOW、ok technical
seed、price_at_or_below entry) を流用し、mode=live + execution broker を注入した runtime を
組む。mock broker なので本番発注は起きない。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from src.analysis.price_analyzer import PriceAnalysis
from src.config.schema import AppConfig, InstrumentConfig, OrchestratorConfig, TradingConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.risk_gate import RiskGateResult
from src.orchestrator.runtime import OrchestratorRuntime
from src.trading.position_manager import Order
from src.utils.clock import db_now

NOW = db_now()
FUTURE = NOW + timedelta(days=1)


class _FakeBroker:
    """execute_signal を記録するだけの mock broker。"""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute_signal(self, signal, position_mgr, macro_context=""):
        self.calls.append(signal)
        return self._result


class _FakePositionMgr:
    """balance だけ返す最小 position manager stub。"""

    def __init__(self, balance: float = 50000.0):
        self._balance = balance

    def get_account_state(self):
        return SimpleNamespace(balance=self._balance)


class _GatePass:
    def pre_check(self, draft, context):
        return RiskGateResult(passed=True)


class _GateReject:
    def __init__(self, reject_class):
        self._rc = reject_class

    def pre_check(self, draft, context):
        return RiskGateResult(passed=False, reject_class=self._rc, issues=["x"])


def _executed_order():
    return Order(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1000.0,
        order_id="mt5:999",
    )


def _app_config():
    return AppConfig(
        trading=TradingConfig(risk_per_trade=0.02, min_lot_size=1000.0, lot_unit=1000.0),
        instruments=[
            InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", pip_value=0.01),
        ],
    )


def _seed_ok_technical(db: Path) -> None:
    AnalysisStore(db).add_snapshot(
        PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.7,
            entry_zone=(149.0, 151.0), reasoning_summary="seed",
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def make_live_runtime(tmp_path: Path, broker, risk_gate, *, mode: str = "live",
                      mid: float = 150.10) -> OrchestratorRuntime:
    """live 執行段を検証する runtime を組む (mode=live, execution broker 注入)。"""
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - 0.005, ask=mid + 0.005, mid=mid, spread=0.01,
            source="test", observed_at=NOW,
        )

    rt = OrchestratorRuntime(
        config=OrchestratorConfig(mode=mode if mode in ("shadow", "live") else "shadow"),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        risk_gate=risk_gate,
        mode=mode,
        execution_broker=broker,
        execution_position_mgr=_FakePositionMgr(),
        app_config=_app_config(),
    )
    return rt


def seed_active_plan_ready_to_trigger(rt: OrchestratorRuntime) -> int:
    """entry が即成立する active plan を 1 件作る (price_at_or_below 150.30, mid=150.10)。"""
    orch = rt._orch
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0, "confidence": 0.7},
        invalidation_json=[],
        expires_at=FUTURE, created_by_run_id=run_id,
    )
