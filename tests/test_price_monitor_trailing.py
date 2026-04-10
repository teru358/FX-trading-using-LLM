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


def test_breakeven_stage_buy_moves_sl_to_entry(tmp_state_store, trailing_buy_pos):
    """BUY: 進捗20%到達でSLはentry(100.0)に上がる。"""
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 進捗20% = 102.0
    _apply_trailing_stop(pos, current=102.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 100.0


def test_breakeven_stage_sell_moves_sl_to_entry(tmp_state_store, trailing_sell_pos):
    """SELL: 進捗20%到達でSLはentry(100.0)に下がる。"""
    mgr = _mk_mgr(tmp_state_store, trailing_sell_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 進捗20% = 98.0
    _apply_trailing_stop(pos, current=98.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 100.0


def test_between_half_and_breakeven_holds_sl_at_midpoint(tmp_state_store, trailing_buy_pos):
    """BUY: 10%〜20%の区間ではSLは半額ステージで上げた中間値のまま据え置き。"""
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # 先に進捗10%で半額ステージに乗せる
    _apply_trailing_stop(pos, current=101.0, cfg=cfg, position_mgr=mgr)
    assert mgr.get_account_state().open_positions[0].stop_loss == 99.5

    # 進捗15% (=101.5): break-even未満 → 半額ステージが再実行されSL=99.5のまま(片方向保証)
    pos2 = mgr.get_account_state().open_positions[0]
    _apply_trailing_stop(pos2, current=101.5, cfg=cfg, position_mgr=mgr)
    assert mgr.get_account_state().open_positions[0].stop_loss == 99.5


def test_follow_stage_buy_dynamic_trail(tmp_state_store, trailing_buy_pos):
    """BUY: 進捗40%到達で動的追従(SL = current - 元SL距離)。
    元SL距離=1.0、current=104.0 → SL=103.0。
    """
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    _apply_trailing_stop(pos, current=104.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 103.0


def test_follow_stage_sell_dynamic_trail(tmp_state_store, trailing_sell_pos):
    """SELL: 進捗40%到達で動的追従。元SL距離=1.0、current=96.0 → SL=97.0。"""
    mgr = _mk_mgr(tmp_state_store, trailing_sell_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    _apply_trailing_stop(pos, current=96.0, cfg=cfg, position_mgr=mgr)

    updated = mgr.get_account_state().open_positions[0]
    assert updated.stop_loss == 97.0


def test_follow_stage_after_breakeven_uses_initial_sl_distance(tmp_state_store, trailing_buy_pos):
    """BUY: break-evenに上がった後でも、follow ステージは元SL距離(1.0)を使う。

    まずSL=entry(100.0)まで上げた後、current=104.0で呼ぶと SL=104-1.0=103.0。
    現在のSL(=100.0)ではなく、initial_stop_loss(=99.0)から計算した元SL距離を使う。
    """
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos = mgr.get_account_state().open_positions[0]
    cfg = _mk_cfg()

    # break-evenに乗せる
    _apply_trailing_stop(pos, current=102.0, cfg=cfg, position_mgr=mgr)
    assert mgr.get_account_state().open_positions[0].stop_loss == 100.0

    # 動的追従: 104.0 - 元SL距離(1.0) = 103.0
    pos2 = mgr.get_account_state().open_positions[0]
    _apply_trailing_stop(pos2, current=104.0, cfg=cfg, position_mgr=mgr)
    assert mgr.get_account_state().open_positions[0].stop_loss == 103.0


def test_initial_stop_loss_preserved_across_updates(tmp_state_store, trailing_buy_pos):
    """SLが更新されても、Order.initial_stop_loss は元の値のまま。"""
    mgr = _mk_mgr(tmp_state_store, trailing_buy_pos)
    pos_before = mgr.get_account_state().open_positions[0]
    assert pos_before.initial_stop_loss == 99.0  # fixtureの元SL
    assert pos_before.stop_loss == 99.0

    cfg = _mk_cfg()
    _apply_trailing_stop(pos_before, current=102.0, cfg=cfg, position_mgr=mgr)

    pos_after = mgr.get_account_state().open_positions[0]
    assert pos_after.stop_loss == 100.0  # break-evenに更新された
    assert pos_after.initial_stop_loss == 99.0  # 元のまま
