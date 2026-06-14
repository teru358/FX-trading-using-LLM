"""price_monitor.run_price_monitor bridge gate behavior."""
from __future__ import annotations


def test_price_monitor_calls_gate_probe_when_provided(tmp_path, monkeypatch):
    """gate が渡されたら冒頭で gate.probe(caller='monitor', sync_balance=False) を呼ぶ。"""
    from unittest.mock import MagicMock
    from src.jobs.price_monitor import run_price_monitor

    config = MagicMock()
    config.price_monitor.enabled = True
    config.state_dir = tmp_path
    config.notifier.enabled = False

    gate = MagicMock()
    gate.probe.return_value = MagicMock(ok=True)
    price_provider = MagicMock()

    monkeypatch.setattr("src.jobs.price_monitor.is_market_open", lambda: True)

    pos_mgr = MagicMock()
    pos_mgr.get_account_state.return_value = MagicMock(open_positions=[MagicMock()])
    monkeypatch.setattr(
        "src.jobs.price_monitor.PositionManager", lambda *a, **kw: pos_mgr,
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.StateStore", lambda _: MagicMock(),
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.monitor_open_positions",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.asyncio.run", lambda coro: None,
    )

    run_price_monitor(config, price_provider, gate)
    gate.probe.assert_called_once_with(caller="monitor", sync_balance=False)


def test_price_monitor_no_gate_skips_probe(tmp_path, monkeypatch):
    """gate=None なら probe は呼ばれない (後方互換)。"""
    from unittest.mock import MagicMock
    from src.jobs.price_monitor import run_price_monitor

    config = MagicMock()
    config.price_monitor.enabled = True
    config.state_dir = tmp_path

    monkeypatch.setattr("src.jobs.price_monitor.is_market_open", lambda: True)

    pos_mgr = MagicMock()
    pos_mgr.get_account_state.return_value = MagicMock(open_positions=[MagicMock()])
    monkeypatch.setattr(
        "src.jobs.price_monitor.PositionManager", lambda *a, **kw: pos_mgr,
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.StateStore", lambda _: MagicMock(),
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.monitor_open_positions",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "src.jobs.price_monitor.asyncio.run", lambda coro: None,
    )

    run_price_monitor(config, MagicMock())
