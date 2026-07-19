"""ExecutionResult の分類を検証する。

execute_signal の戻り値を ExecutionResult
(executed/skipped/halted/rejected/failed) に分類することを検証する。
(旧 skip 通知の文面テストは Task 8 の取引サイクル退役で通知経路ごと削除した。)
"""
from __future__ import annotations

from src.trading.broker_adapter import ExecutionResult


# ── ExecutionResult 型 ──────────────────────────────────────────

def test_executed_result_is_executed_and_carries_order():
    sentinel = object()
    r = ExecutionResult.executed(sentinel)
    assert r.is_executed is True
    assert r.outcome == "executed"
    assert r.order is sentinel


def test_rejected_result_is_not_executed():
    r = ExecutionResult.rejected("retcode=10016 Invalid stops")
    assert r.is_executed is False
    assert r.outcome == "rejected"
    assert r.order is None
    assert "10016" in r.reason


def test_skipped_halted_failed_factories():
    assert ExecutionResult.skipped("x").outcome == "skipped"
    assert ExecutionResult.halted("x").outcome == "halted"
    assert ExecutionResult.failed("x").outcome == "failed"
