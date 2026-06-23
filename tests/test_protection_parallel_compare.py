from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.data.orchestrator_store import OrchestratorStore
from src.jobs.price_monitor import _apply_profit_protection
from src.orchestrator.context_builder import QuoteSnapshot
from src.orchestrator.position_protection_worker import PriceProtectionWorker
from src.trading.position_manager import Order
from src.utils.clock import db_now


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos() -> Order:
    # 同一 order_id を両 source で使う (比較は order_id でペアリング)
    o = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1.0,
    )
    o.order_id = "o1"
    return o


class _PosMgr:
    def update_protection_state(self, *a, **k): ...
    def clear_pending_protection_target(self, *a, **k): ...
    def update_stop_loss(self, *a, **k): ...


class _Producer:
    def latest(self, pair):
        return QuoteSnapshot(
            bid=150.6, ask=150.6, mid=150.6, spread=None,
            source="mt5", observed_at=datetime.now(),
        )


def test_price_monitor_and_worker_agree_on_same_position(tmp_path: Path):
    """同一局面 (current=150.6) で両 source の action が一致する (spec §5.4)。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()

    # price_monitor 経路 (legacy で実行するが broker=None で副作用なし) + 記録
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=store,
    )
    # tick worker 経路 (protect_shadow = execute=False で記録のみ)
    worker = PriceProtectionWorker(
        producer=_Producer(), position_provider=lambda: [_pos()],
        store=store, cfg=_cfg(), position_mgr=_PosMgr(), broker=None,
        mode="protect_shadow",
    )
    worker.run_once()

    rows = store.compare_protection_decisions(
        since=now - timedelta(minutes=5), max_delta_seconds=60
    )
    assert len(rows) == 1
    assert rows[0]["action_match"] is True
