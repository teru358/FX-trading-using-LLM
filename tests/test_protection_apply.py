from types import SimpleNamespace

from src.trading.protection_apply import apply_protection, ProtectionApplyResult
from src.trading.position_manager import Order


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos() -> Order:
    return Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1.0,
    )


class _PosMgr:
    def __init__(self):
        self.protection_state_calls = []
        self.sl_updates = []
        self.pending_cleared = []

    def update_protection_state(self, order_id, **kw):
        self.protection_state_calls.append((order_id, kw))

    def update_stop_loss(self, order_id, new_sl, stage=None):
        self.sl_updates.append((order_id, new_sl, stage))
        return True

    def clear_pending_protection_target(self, order_id):
        self.pending_cleared.append(order_id)


def test_apply_protection_updates_mfe_state_and_sl():
    """helper が MFE state 更新 + SL 適用の副作用一式を行う (review H-b)。"""
    mgr = _PosMgr()
    res = apply_protection(
        _pos(), current=150.6, cfg=_cfg(), position_mgr=mgr,
        broker=None, remote_sync_enabled=False, execute=True,
    )
    assert isinstance(res, ProtectionApplyResult)
    # MFE state は必ず更新される (適用有無に関わらず)
    assert len(mgr.protection_state_calls) >= 1
    # +0.6R → breakeven (entry) raise_sl が SL 適用される
    assert len(mgr.sl_updates) == 1
    assert res.action == "raise_sl"


def test_apply_protection_execute_false_records_no_side_effects():
    """execute=False (shadow) は判定だけ返し SL 適用も state 更新もしない。"""
    mgr = _PosMgr()
    res = apply_protection(
        _pos(), current=150.6, cfg=_cfg(), position_mgr=mgr,
        broker=None, remote_sync_enabled=False, execute=False,
    )
    assert res.action == "raise_sl"          # 判定は返る
    assert mgr.sl_updates == []              # SL 適用なし
    assert mgr.protection_state_calls == []  # state 更新もなし (純粋判定)


def test_apply_protection_close_not_executed():
    """close は実行しない (H4)。action=close でも SL 適用は走らない。"""
    pos = _pos()
    pos.max_favorable_r = 1.0
    pos.max_favorable_price = 151.0
    mgr = _PosMgr()
    res = apply_protection(
        pos, current=150.1, cfg=_cfg(), position_mgr=mgr,
        broker=None, remote_sync_enabled=False, execute=True,
    )
    assert res.action == "close"
    assert mgr.sl_updates == []  # close は SL 適用も close 実行もしない
