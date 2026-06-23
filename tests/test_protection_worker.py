from datetime import datetime
from types import SimpleNamespace

from src.orchestrator.position_protection_worker import PriceProtectionWorker
from src.trading.position_manager import Order


class _RecordingStore:
    def __init__(self):
        self.records = []

    def record_protection_decision(self, **kw):
        self.records.append(kw)


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos(*, entry=150.0, sl=149.0, take_profit=152.0, max_fav_r=0.0,
         max_fav_price=None) -> Order:
    o = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=entry,
        stop_loss=sl, take_profit=take_profit, position_size=1.0,
    )
    o.max_favorable_r = max_fav_r
    o.max_favorable_price = max_fav_price
    return o


class _PosMgr:
    def __init__(self):
        self.sl_updates = []

    def update_protection_state(self, *a, **k): ...
    def clear_pending_protection_target(self, *a, **k): ...

    def update_stop_loss(self, order_id, new_sl, stage=None):
        self.sl_updates.append((order_id, new_sl, stage))
        return True


class _Producer:
    def __init__(self, mid):
        self._mid = mid

    def latest(self, pair):
        from src.orchestrator.context_builder import QuoteSnapshot
        return QuoteSnapshot(
            bid=self._mid, ask=self._mid, mid=self._mid, spread=None,
            source="mt5", observed_at=datetime.now(),
        )


def _worker(producer, positions, store, mgr, broker, mode):
    return PriceProtectionWorker(
        producer=producer, position_provider=lambda: positions,
        store=store, cfg=_cfg(), position_mgr=mgr, broker=broker,
        mode=mode, remote_sync_enabled=True,
    )


def test_shadow_records_decision_without_executing():
    store = _RecordingStore()
    mgr = _PosMgr()
    worker = _worker(
        _Producer(mid=150.6), [_pos()], store, mgr, broker=None,
        mode="protect_shadow",
    )
    worker.run_once()
    assert len(store.records) == 1
    assert store.records[0]["source"] == "tick_worker"
    assert mgr.sl_updates == []  # protect_shadow は execute=False → SL 適用なし


def test_close_action_is_not_executed():
    """giveback で action=close になっても worker は実行しない (H4)。"""
    store = _RecordingStore()
    mgr = _PosMgr()
    # MFE 1.0R 記録済み (max_fav_price=151.0) で現在 150.1 → giveback ≈ 0.9R ≥ 0.4 かつ
    # MFE 1.0R ≥ min(0.8) → close。
    pos = _pos(max_fav_r=1.0, max_fav_price=151.0)
    worker = _worker(
        _Producer(mid=150.1), [pos], store, mgr, broker=None,
        mode="protect_live",  # live でも close は適用しない
    )
    worker.run_once()
    assert mgr.sl_updates == []  # close は SL 適用ゼロ


def test_off_or_producer_mode_does_nothing():
    store = _RecordingStore()
    mgr = _PosMgr()
    worker = _worker(
        _Producer(mid=150.6), [_pos()], store, mgr, broker=None, mode="producer",
    )
    worker.run_once()
    assert store.records == []  # producer 段では保護 worker は何もしない


def test_protect_live_applies_sl_via_helper():
    """protect_live は execute=True で position_mgr.update_stop_loss が走る (helper 副作用)。"""
    store = _RecordingStore()
    mgr = _PosMgr()
    worker = _worker(
        _Producer(mid=150.6), [_pos()], store, mgr, broker=None,
        mode="protect_live",
    )
    worker.run_once()
    assert len(mgr.sl_updates) == 1  # +0.6R → breakeven raise_sl が helper 経由で適用
