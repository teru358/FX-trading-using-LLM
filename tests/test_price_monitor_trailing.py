"""price_monitor._apply_trailing_stop の段階型ロジックをテスト。"""
from __future__ import annotations

from src.config import PriceMonitorConfig
from src.jobs.price_monitor import _apply_trailing_stop
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager


def _mk_cfg() -> PriceMonitorConfig:
    return PriceMonitorConfig(
        enabled=True,
        trailing_stop_enabled=True,
        trailing_stop_breakeven_pct=0.20,
        trailing_stop_activation_pct=0.40,
        trailing_stop_distance_ratio=1.0,
    )


def _mk_mgr(tmp_state_store: StateStore, order) -> PositionManager:
    mgr = PositionManager(tmp_state_store, initial_balance=100_000.0, context="TrailTest")
    mgr.open_position(order)
    return mgr


def test_half_stage_buy_updates_sl_to_midpoint(tmp_state_store, trailing_buy_pos):
    """BUY: 進捗10%到達でSLはentryと元SLの中間(99.5)に上がる。"""
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 進捗10% = entry=100 + 10×0.10 = 101.0
    _apply_trailing_stop(pos, current=101.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 99.5


def test_half_stage_sell_updates_sl_to_midpoint(tmp_state_store, trailing_sell_pos):
    """SELL: 進捗10%到達でSLはentryと元SLの中間(100.5)に下がる。"""
    mgr = _mk_mgr(tmp_state_store, trailing_sell_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 進捗10% = entry=100 - 10×0.10 = 99.0
    _apply_trailing_stop(pos, current=99.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 100.5


def test_below_half_stage_does_not_update(tmp_state_store, trailing_buy_pos):
    """BUY: 進捗10%未満ではSLは元のまま(99.0)。"""
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 進捗9% = 100.9
    _apply_trailing_stop(pos, current=100.9, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 99.0
