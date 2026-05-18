"""ShadowBrokerAdapter のユニットテスト。

primary 結果を canonical として返し、observer 結果を JSONL に追記すること、
observer 失敗が primary に影響しないこと、observer が None を返した時にも
ログが書かれることを検証する。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.trading.broker_adapter import ExecutionResult
from src.trading.position_manager import Order
from src.trading.shadow_broker import ShadowBrokerAdapter


def _mk_order(pair: str = "USDJPY=X", entry: float = 159.0,
              oid: str = "primary-1") -> Order:
    o = Order.new(pair, "buy", entry, 158.0, 160.0, 1000.0)
    o.order_id = oid
    return o


def test_execute_signal_returns_primary_result(tmp_path: Path):
    primary = MagicMock()
    observer = MagicMock()
    primary.execute_signal.return_value = ExecutionResult.executed(
        _mk_order(entry=159.0, oid="primary-1"))
    observer.execute_signal.return_value = ExecutionResult.executed(
        _mk_order(entry=159.5, oid="mt5:42"))

    adapter = ShadowBrokerAdapter(
        primary=primary, observer=observer,
        observer_position_mgr=MagicMock(),
        comparison_log_path=tmp_path / "shadow.jsonl",
    )
    primary_pm = MagicMock()
    sig = MagicMock(pair="USDJPY=X", action="buy")

    result = adapter.execute_signal(sig, primary_pm)

    assert result.is_executed
    assert result.order.order_id == "primary-1"   # primary が常に canonical
    primary.execute_signal.assert_called_once()
    observer.execute_signal.assert_called_once()

    log = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8")
    rec = json.loads(log.strip())
    assert rec["event"] == "execute"
    assert rec["primary"]["order_id"] == "primary-1"
    assert rec["observer"]["order_id"] == "mt5:42"
    assert rec["price_diff"] == pytest.approx(0.5)


def test_observer_failure_does_not_break_primary(tmp_path: Path, caplog):
    primary = MagicMock()
    observer = MagicMock()
    primary.execute_signal.return_value = ExecutionResult.executed(_mk_order())
    observer.execute_signal.side_effect = RuntimeError("bridge down")

    adapter = ShadowBrokerAdapter(
        primary=primary, observer=observer,
        observer_position_mgr=MagicMock(),
        comparison_log_path=tmp_path / "shadow.jsonl",
    )
    sig = MagicMock(pair="USDJPY=X", action="buy")
    with caplog.at_level("WARNING"):
        result = adapter.execute_signal(sig, MagicMock())

    assert result.is_executed    # primary は成功
    assert any("observer" in r.message.lower() for r in caplog.records)


def test_observer_skip_logs_event_with_null_order(tmp_path: Path):
    primary = MagicMock()
    observer = MagicMock()
    primary.execute_signal.return_value = ExecutionResult.executed(_mk_order())
    # observer が発注せず skip した場合 (order is None)
    observer.execute_signal.return_value = ExecutionResult.skipped(
        "portfolio gate でブロック")

    adapter = ShadowBrokerAdapter(
        primary=primary, observer=observer,
        observer_position_mgr=MagicMock(),
        comparison_log_path=tmp_path / "shadow.jsonl",
    )
    sig = MagicMock(pair="USDJPY=X", action="buy")
    adapter.execute_signal(sig, MagicMock())

    rec = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
    assert rec["primary"] is not None
    assert rec["observer"] is None
    assert rec["price_diff"] is None


def test_check_and_close_logs_when_either_side_closes(tmp_path: Path):
    primary = MagicMock()
    observer = MagicMock()
    closed_p = _mk_order(oid="primary-c")
    closed_o = _mk_order(oid="mt5:99")
    primary.check_and_close_positions.return_value = [closed_p]
    observer.check_and_close_positions.return_value = [closed_o]

    obs_pm = MagicMock()
    obs_pm.get_account_state.return_value = MagicMock(open_positions=[])

    adapter = ShadowBrokerAdapter(
        primary=primary, observer=observer,
        observer_position_mgr=obs_pm,
        comparison_log_path=tmp_path / "shadow.jsonl",
    )
    result = adapter.check_and_close_positions(
        open_positions=[], current_prices={"USDJPY=X": 159.0},
        position_mgr=MagicMock(),
    )

    assert result == [closed_p]   # primary の close リストを返す
    rec = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
    assert rec["event"] == "close_check"
    assert len(rec["primary_closed"]) == 1
    assert len(rec["observer_closed"]) == 1


def test_check_and_close_no_log_when_both_empty(tmp_path: Path):
    primary = MagicMock()
    observer = MagicMock()
    primary.check_and_close_positions.return_value = []
    observer.check_and_close_positions.return_value = []
    obs_pm = MagicMock()
    obs_pm.get_account_state.return_value = MagicMock(open_positions=[])

    log_path = tmp_path / "shadow.jsonl"
    adapter = ShadowBrokerAdapter(
        primary=primary, observer=observer,
        observer_position_mgr=obs_pm,
        comparison_log_path=log_path,
    )
    result = adapter.check_and_close_positions(
        open_positions=[], current_prices={}, position_mgr=MagicMock(),
    )

    assert result == []
    # 両方空なら JSONL は作成されない (ノイズ低減)
    assert not log_path.exists()
