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
            return PairAnalysisOutcome(signal=sig_ok, macro_ctx="m")
        if pair_cfg.symbol == "EURUSD=X":
            return PairAnalysisError(pair="EURUSD=X", error=RuntimeError("boom"))
        return None  # スナップショット未取得によるスキップ

    monkeypatch.setattr("src.cycles.trading._process_pair", fake_process)

    config = MagicMock()
    config.llm.provider_config.max_concurrent = 2
    skipped_pair = MagicMock(symbol="GBPUSD=X")
    skipped_pair.display_name = "GBPUSD=X"
    config.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X"), MagicMock(symbol="EURUSD=X"), skipped_pair,
    ]
    signals, macro_ctxs, data_health = await _phase_analyze_pairs(
        config, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), None,
    )
    assert signals == [sig_ok]
    assert macro_ctxs == {"USDJPY=X": "m"}
    assert any("EURUSD=X 分析失敗" in d for d in data_health)
    assert any("GBPUSD=X" in d and "スキップ" in d for d in data_health)


def test_format_decision_line_rag_demotes_sell_to_hold():
    """SELL が RAG 補正で HOLD に降格したケースを1行で表現する (案1)。"""
    from src.notifications.notifier import SignalOutcome, format_decision_line

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.77, combined_score=-0.127,
        reason="score in deadband (-0.127, db=0.150)",
        detail_reason="d", news_score=-0.24, tech_score=-0.23,
        rag_note="score -0.231→-0.127, sell→hold",
    )
    line = format_decision_line(o, pre_action="sell", pre_score=-0.231)
    assert "[DECISION]" in line
    assert "EUR/USD" in line or "EURUSD=X" in line
    # pre 判定 → RAG → final 判定 が読み取れる
    assert "SELL" in line and "-0.231" in line
    assert "RAG" in line
    assert "HOLD" in line and "-0.127" in line
    # 降格理由と confidence
    assert "deadband" in line
    assert "0.77" in line


def test_format_decision_line_executed_no_rag_change():
    """RAG 補正で action が変わらず発注されたケース (rag_note 空 or score のみ)。"""
    from src.notifications.notifier import SignalOutcome, format_decision_line

    o = SignalOutcome(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.86, combined_score=0.18,
        reason="score=+0.180 conf=0.86",
        detail_reason="d", news_score=0.06, tech_score=0.45,
        rag_note="score +0.283→+0.180",
    )
    line = format_decision_line(o, pre_action="buy", pre_score=0.283)
    assert "[DECISION]" in line
    assert "BUY" in line
    assert "EXECUTED" in line
    assert "0.283" in line and "0.180" in line


def test_format_decision_line_no_rag_adjustment():
    """RAG 補正なし (rag_note 空) のときは RAG セグメントを出さない。"""
    from src.notifications.notifier import SignalOutcome, format_decision_line

    o = SignalOutcome(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.86, combined_score=0.283,
        reason="score=+0.283 conf=0.86",
        detail_reason="d", news_score=0.06, tech_score=0.45,
        rag_note="",
    )
    line = format_decision_line(o, pre_action="buy", pre_score=0.283)
    assert "RAG" not in line
    assert "BUY" in line and "EXECUTED" in line


def test_classify_hold_reasons_rag_demote():
    """RAG 補正がなければ BUY/SELL だったが HOLD 化 → rag_demote フラグ (案4の核心)。"""
    from src.notifications.notifier import SignalOutcome, classify_hold_reasons

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.77, combined_score=-0.127,
        reason="score in deadband (-0.127, db=0.150)",
        detail_reason="d", news_score=-0.24, tech_score=-0.23,
        rag_note="score -0.231→-0.127, sell→hold",
    )
    flags = classify_hold_reasons(o, pre_action="sell")
    assert "rag_demote" in flags


def test_classify_hold_reasons_confidence_and_conflict():
    from src.notifications.notifier import SignalOutcome, classify_hold_reasons

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.51, combined_score=0.18,
        reason="confidence too low (0.51 < 0.6) [NEWS/PRICE conflict]",
        detail_reason="d", news_score=0.62, tech_score=0.07,
        rag_note="",
    )
    flags = classify_hold_reasons(o, pre_action="hold")
    assert "confidence_low" in flags
    assert "news_price_conflict" in flags
    assert "rag_demote" not in flags  # pre も hold なので降格ではない


def test_classify_hold_reasons_accuracy_gate():
    from src.notifications.notifier import SignalOutcome, classify_hold_reasons

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.59, combined_score=-0.067,
        reason="forecast accuracy below hard_threshold",
        detail_reason="d", news_score=-0.01, tech_score=-0.08,
        rag_note="",
    )
    flags = classify_hold_reasons(o, pre_action="hold")
    assert "accuracy_gate" in flags


def test_classify_hold_reasons_deadband_no_rag():
    from src.notifications.notifier import SignalOutcome, classify_hold_reasons

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.61, combined_score=-0.115,
        reason="score in deadband (-0.115, db=0.180)",
        detail_reason="d", news_score=-0.29, tech_score=-0.07,
        rag_note="",
    )
    flags = classify_hold_reasons(o, pre_action="hold")
    assert "deadband" in flags
    assert "rag_demote" not in flags


def test_format_health_line_executed_is_concise():
    """発注された銘柄は EXECUTED とだけ示す (HOLD要因は出さない)。"""
    from src.notifications.notifier import SignalOutcome, format_health_line

    o = SignalOutcome(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.86, combined_score=0.18,
        reason="score=+0.180 conf=0.86",
        detail_reason="d", news_score=0.06, tech_score=0.45,
        rag_note="score +0.283→+0.180",
    )
    line = format_health_line(o, pre_action="buy")
    assert "[HEALTH]" in line
    assert "USDJPY=X" in line
    assert "EXECUTED" in line


def test_format_health_line_hold_lists_flags():
    from src.notifications.notifier import SignalOutcome, format_health_line

    o = SignalOutcome(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.77, combined_score=-0.127,
        reason="score in deadband (-0.127, db=0.150)",
        detail_reason="d", news_score=-0.24, tech_score=-0.23,
        rag_note="score -0.231→-0.127, sell→hold",
    )
    line = format_health_line(o, pre_action="sell")
    assert "[HEALTH]" in line
    assert "EURUSD=X" in line
    assert "rag_demote" in line
    assert "HOLD" in line


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


@pytest.mark.asyncio
async def test_execute_one_signal_atr_demoted_fallback_uses_original_action(monkeypatch):
    """ATR降格 + notify_on_cycle_summary=False の旧通知で action が元の buy/sell のまま。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult

    monkeypatch.setattr("src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: None)
    sig = _exec_signal(action="buy")
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.skipped("hold (発注対象外)")
    notifier = MagicMock()
    notifier.notify_signal_skipped = AsyncMock()

    await _execute_one_signal(
        sig, "", _exec_config(notify_on_cycle_summary=False), MagicMock(), broker,
        notifier, MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    notifier.notify_signal_skipped.assert_awaited_once()
    event = notifier.notify_signal_skipped.call_args.args[0]
    assert event.action == "buy"
