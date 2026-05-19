"""取引サイクル集約通知の配線テスト (分析 Phase / 実行 Phase / halt サマリー)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_pair_analysis_outcome_and_error_construct():
    from src.cycles.trading import PairAnalysisError, PairAnalysisOutcome

    out = PairAnalysisOutcome(signal=MagicMock(), macro_ctx="m")
    assert out.tech_fallback is False
    err = PairAnalysisError(pair="USDJPY=X", error=RuntimeError("x"))
    assert err.pair == "USDJPY=X"


@pytest.mark.asyncio
async def test_phase_analyze_pairs_collects_data_health(monkeypatch):
    from src.cycles.trading import (
        PairAnalysisError,
        PairAnalysisOutcome,
        _phase_analyze_pairs,
    )

    sig_ok = MagicMock(pair="USDJPY=X")

    async def fake_process(pair_cfg, *a, **k):
        if pair_cfg.symbol == "USDJPY=X":
            return PairAnalysisOutcome(signal=sig_ok, macro_ctx="m", tech_fallback=True)
        return PairAnalysisError(pair="EURUSD=X", error=RuntimeError("boom"))

    monkeypatch.setattr("src.cycles.trading._process_pair", fake_process)

    config = MagicMock()
    config.llm.provider_config.max_concurrent = 2
    config.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X"), MagicMock(symbol="EURUSD=X"),
    ]
    signals, macro_ctxs, data_health = await _phase_analyze_pairs(
        config, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), None,
    )
    assert signals == [sig_ok]
    assert macro_ctxs == {"USDJPY=X": "m"}
    assert any("EURUSD=X 分析失敗" in d for d in data_health)
    assert any("USDJPY=X" in d and "fallback" in d for d in data_health)


def _exec_signal(action: str = "buy") -> MagicMock:
    """_execute_one_signal / _phase_execute_signals 用の signal モック。"""
    s = MagicMock()
    s.pair = "USDJPY=X"
    s.action = action
    s.confidence = 0.75
    s.combined_score = 0.32
    s.signal_reason = "rates higher"
    s.detail_reason = "detail"
    s.entry_price = 159.0
    s.stop_loss = 158.0
    s.take_profit = 161.0
    s.position_size = 1000.0
    s.predicted_direction = "bullish"
    s.tv_recommendation = "BUY"
    s.news = MagicMock(sentiment_score=0.12)
    s.price = MagicMock(bias_score=0.37)
    return s


def _exec_config(notify_on_cycle_summary: bool = True) -> MagicMock:
    c = MagicMock()
    c.notifier.notify_on_cycle_summary = notify_on_cycle_summary
    c.notifier.notify_on_order_open = True
    c.notifier.notify_on_signal_skipped = True
    return c


@pytest.mark.asyncio
async def test_execute_one_signal_atr_failure_skipped_with_atr_reason(monkeypatch):
    """ATR SL/TP 失敗で hold 降格 → status=skipped, reason は ATR 理由 (hold文言にしない)。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult

    monkeypatch.setattr("src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: None)
    sig = _exec_signal(action="buy")
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.skipped("hold (発注対象外)")

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "skipped"
    assert outcome.action == "buy"
    assert outcome.reason == "ATR SL/TP calculation failed"
    assert "発注対象外" not in outcome.reason


@pytest.mark.asyncio
async def test_execute_one_signal_executed_returns_outcome_with_order(monkeypatch):
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "executed"
    assert outcome.order is order
    assert outcome.news_score == 0.12
    assert outcome.tech_score == 0.37
    assert outcome.tv_recommendation == "BUY"


@pytest.mark.asyncio
async def test_execute_one_signal_fallback_fires_old_notification(monkeypatch):
    """notify_on_cycle_summary=False なら旧 notify_order_opened が発火する。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)
    notifier = MagicMock()
    notifier.notify_order_opened = AsyncMock()

    await _execute_one_signal(
        sig, "", _exec_config(notify_on_cycle_summary=False), MagicMock(), broker,
        notifier, MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    notifier.notify_order_opened.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_one_signal_no_old_notification_when_summary_enabled(monkeypatch):
    """notify_on_cycle_summary=True なら旧 notify_order_opened は発火しない。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)
    notifier = MagicMock()
    notifier.notify_order_opened = AsyncMock()

    await _execute_one_signal(
        sig, "", _exec_config(notify_on_cycle_summary=True), MagicMock(), broker,
        notifier, MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    notifier.notify_order_opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_one_signal_broker_rejection_stays_rejected(monkeypatch):
    """MT5 拒否 (ExecutionResult.rejected) は status=rejected のまま (skipped に落とさない)。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="sell")
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.rejected(
        "発注拒否 (broker): retcode=10016 Invalid stops")

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "rejected"
    assert "retcode=10016" in outcome.reason


@pytest.mark.asyncio
async def test_halt_cycle_sends_halt_summary(tmp_path, monkeypatch):
    """halt 中サイクルは halted=True の CycleSummaryEvent を1回送る。"""
    from src.cycles.trading import trading_cycle
    from src.persistence import halt_state

    halt_state.trigger_manual(tmp_path, reason="test")
    notifier = MagicMock(notify_cycle_summary=AsyncMock())
    monkeypatch.setattr(
        "src.cycles.trading._build_trading_runtime",
        lambda c: (MagicMock(), MagicMock(), notifier,
                   MagicMock(model_name="m"), MagicMock(model_name="r")),
    )
    monkeypatch.setattr("src.cycles.trading._phase_close_sl_tp", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.cycles.trading._finalize_closed_orders", AsyncMock(return_value=None))
    monkeypatch.setattr("src.cycles.trading._review_hold_decisions", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.cycles.trading._phase_review_open_positions", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.cycles.trading.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr("src.cycles.trading.local_now", lambda c: datetime(2026, 5, 19, 17, 30))
    monkeypatch.setattr("src.cycles.trading.make_embed_fn", lambda c: lambda x: [])
    monkeypatch.setattr("src.cycles.trading.print_run_summary", lambda **kw: None)

    config = MagicMock()
    config.state_dir = tmp_path
    config.mode = "paper"
    config.notifier.notify_on_cycle_summary = True

    pm = MagicMock()
    pm.get_account_state.return_value = MagicMock(balance=10000.0)
    await trading_cycle(
        config, pm, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        price_provider=MagicMock(), session_store=MagicMock(),
    )
    notifier.notify_cycle_summary.assert_awaited_once()
    event = notifier.notify_cycle_summary.call_args.args[0]
    assert event.halted is True
