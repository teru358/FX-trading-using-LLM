from types import SimpleNamespace

from src.jobs.price_monitor import _apply_profit_protection
from src.trading.position_manager import Order


class _RecStore:
    def __init__(self):
        self.records = []

    def record_protection_decision(self, **kw):
        self.records.append(kw)


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
    def update_protection_state(self, *a, **k): ...
    def clear_pending_protection_target(self, *a, **k): ...
    def update_stop_loss(self, *a, **k): return True


def test_records_to_store_when_store_provided():
    store = _RecStore()
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=store,
    )
    assert len(store.records) == 1
    assert store.records[0]["source"] == "price_monitor"


def test_no_record_when_store_none():
    # store 未指定 (off/producer 段) では完全無改変 = 記録しない
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=None,
    )
    # 例外なく完了すれば OK (記録対象 store が無い)
