"""Task F (codex #3): orchestrator.mode=live のとき旧 trading cycle の新規 entry phase
(_phase_execute_signals) が execute_signal を呼ばない (single execution writer)。

全 entry point (main/API/CLI/TUI) は run_trading_cycle → _phase_execute_signals を通るため、
この phase をガードすれば一括カバーされる。shadow (既定) では従来通り entry を実行する。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cycles import trading


class _Broker:
    def __init__(self):
        self.calls = []

    def execute_signal(self, sig, position_mgr, macro_context=""):
        self.calls.append(sig)
        from src.trading.broker_adapter import ExecutionResult
        return ExecutionResult.skipped("test")

    def is_halted(self):
        return False


def _run_phase(orch_mode: str, broker):
    """_phase_execute_signals を最小依存で呼ぶ。orchestrator.mode のみ振る。"""
    from src.config.schema import AppConfig, OrchestratorConfig

    config = AppConfig(orchestrator=OrchestratorConfig(mode=orch_mode))
    # 1 件の buy シグナル (entry 候補)。
    sig = SimpleNamespace(action="buy", combined_score=0.5, pair="USDJPY=X")
    return asyncio.run(
        trading._phase_execute_signals(
            signals=[sig], macro_ctxs={}, config=config,
            position_mgr=MagicMock(), broker=broker, notifier=MagicMock(),
            store=MagicMock(), price_store=MagicMock(), hold_store=MagicMock(),
            session_store=MagicMock(), adaptive_store=MagicMock(),
            embed_fn_adj=MagicMock(), price_provider=None,
        )
    )


def test_entry_skipped_when_orchestrator_live():
    """orchestrator.mode=live: 新規 entry phase が即 return し execute_signal を呼ばない。"""
    broker = _Broker()
    executed, outcomes = _run_phase("live", broker)
    assert broker.calls == []        # 発注なし (single writer は orchestrator 側)
    assert executed == []
