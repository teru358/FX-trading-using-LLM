"""trading_cycle 入口の halt チェックテスト (新規エントリー分析の前)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_trading_cycle_skips_phase_analyze_when_halted(tmp_path, monkeypatch):
    """halt 中は Phase 3 (_phase_analyze_pairs) 以降が呼ばれない。
    Phase 1〜2.5 (close/reflection/hold review) は継続する。
    """
    from src.persistence import halt_state
    halt_state.trigger_manual(tmp_path, reason="test")

    config = MagicMock()
    config.state_dir = tmp_path
    config.mode = "paper"

    # Phase 1, 1.5, 2.5 はモックで no-op に
    monkeypatch.setattr(
        "src.cycles.trading._phase_close_sl_tp",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.cycles.trading._finalize_closed_orders",
        AsyncMock(return_value=None),
    )
    review_hold_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "src.cycles.trading._review_hold_decisions",
        review_hold_mock,
    )
    # Phase 3 系: 呼ばれてはいけない
    analyze_mock = AsyncMock(return_value=([], {}))
    monkeypatch.setattr(
        "src.cycles.trading._phase_analyze_pairs", analyze_mock,
    )
    review_open_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "src.cycles.trading._phase_review_open_positions", review_open_mock,
    )
    execute_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "src.cycles.trading._phase_execute_signals", execute_mock,
    )
    # _build_trading_runtime / market_open / make_embed_fn / print_run_summary は副次
    monkeypatch.setattr(
        "src.cycles.trading._build_trading_runtime",
        lambda c: (MagicMock(), MagicMock(), MagicMock(), MagicMock(model_name="m"), MagicMock(model_name="r")),
    )
    monkeypatch.setattr("src.cycles.trading.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr("src.cycles.trading.local_now", lambda c: datetime(2026, 5, 7, 12, 0))
    monkeypatch.setattr("src.cycles.trading.make_embed_fn", lambda c: lambda x: [])
    monkeypatch.setattr("src.cycles.trading.print_run_summary", lambda **kw: None)

    from src.cycles.trading import trading_cycle
    pm = MagicMock()
    pm.get_account_state.return_value = MagicMock(balance=10000.0)
    await trading_cycle(
        config, pm, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        price_provider=MagicMock(), session_store=MagicMock(),
    )

    # Phase 2.5 (review_hold) は走った
    review_hold_mock.assert_awaited_once()
    # Phase 3〜4b は skip
    analyze_mock.assert_not_called()
    review_open_mock.assert_not_called()
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_trading_cycle_runs_phase_analyze_when_not_halted(tmp_path, monkeypatch):
    """halt 解除中は Phase 3 が呼ばれる (regression guard)。"""
    config = MagicMock()
    config.state_dir = tmp_path  # halt.json 不在 = 解除
    config.mode = "paper"

    monkeypatch.setattr(
        "src.cycles.trading._phase_close_sl_tp",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.cycles.trading._finalize_closed_orders",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.cycles.trading._review_hold_decisions",
        AsyncMock(return_value=None),
    )
    analyze_mock = AsyncMock(return_value=([], {}))
    monkeypatch.setattr(
        "src.cycles.trading._phase_analyze_pairs", analyze_mock,
    )
    monkeypatch.setattr(
        "src.cycles.trading._phase_review_open_positions",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.cycles.trading._phase_execute_signals",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.cycles.trading._build_trading_runtime",
        lambda c: (MagicMock(), MagicMock(), MagicMock(), MagicMock(model_name="m"), MagicMock(model_name="r")),
    )
    monkeypatch.setattr("src.cycles.trading.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr("src.cycles.trading.local_now", lambda c: datetime(2026, 5, 7, 12, 0))
    monkeypatch.setattr("src.cycles.trading.make_embed_fn", lambda c: lambda x: [])
    monkeypatch.setattr("src.cycles.trading.print_run_summary", lambda **kw: None)

    from src.cycles.trading import trading_cycle
    pm = MagicMock()
    pm.get_account_state.return_value = MagicMock(balance=10000.0)
    await trading_cycle(
        config, pm, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        price_provider=MagicMock(), session_store=MagicMock(),
    )

    # Phase 3 走った
    analyze_mock.assert_awaited_once()
