"""protect_shadow 以上で保護 worker が runtime に配線される lifecycle テスト (spec §5.2)。

producer と protection_worker が runtime の start/stop で正しく起動・停止することを検証する。
worker は producer.latest を消費するため start は producer→worker、stop は worker→producer の順。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime


def _make_minimal_runtime(tmp_path, *, quote_producer=None, protection_worker=None):
    """enabled=True の最小 valid OrchestratorRuntime を組む。

    test_orchestrator_runtime.test_start_spawns_threads_when_enabled_and_stop_joins を
    モデルに、実 DB / context_builder / stub quote_provider で構築する。producer/worker の
    lifecycle hook 発火確認だけが目的なので detector/pipeline 等は注入しない。
    """
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.02, mid=150.01, spread=0.02,
            source="test", observed_at=datetime(2026, 6, 20, 12, 0, 0),
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(enabled=True),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        quote_producer=quote_producer,
        protection_worker=protection_worker,
    )


def _seed_balance(state_dir: Path) -> None:
    """PositionManager が balance.json を読めるよう最小残高を seed する。"""
    from datetime import datetime, timezone

    from src.persistence.balance_snapshot import BalanceSnapshot, write as write_balance

    write_balance(
        state_dir,
        BalanceSnapshot(
            balance=100_000.0, deposit=100_000.0, peak_balance=100_000.0,
            source="paper",
            fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def test_position_manager_reload_picks_up_disk_changes(tmp_path: Path) -> None:
    """別インスタンスが disk に書いたポジションは reload() で初めて見える (Codex High FIX 2)。"""
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import Order, PositionManager

    sd = tmp_path
    _seed_balance(sd)
    mgr_a = PositionManager(StateStore(sd), context="A")
    assert mgr_a.get_account_state().open_positions == []

    # 別インスタンスがポジションを建てて disk に書く (trade cycle 相当)。
    mgr_b = PositionManager(StateStore(sd), context="B")
    mgr_b.open_position(
        Order.new(
            pair="USDJPY=X", direction="buy", entry_price=150.0,
            stop_loss=149.0, take_profit=152.0, position_size=10000.0,
            signal_reason="test",
        )
    )

    # mgr_a は reload するまで in-memory state のまま (新規ポジションは見えない)。
    assert mgr_a.get_account_state().open_positions == []
    mgr_a.reload()
    assert len(mgr_a.get_account_state().open_positions) == 1
    assert mgr_a.get_account_state().open_positions[0].pair == "USDJPY=X"


def test_protection_provider_reflects_positions_added_after_creation(
    tmp_path: Path,
) -> None:
    """bootstrap の reload-then-read provider は作成後に disk へ追加されたポジを反映する。

    daemon 起動時はポジゼロ、その後 trade cycle が別インスタンスで建てる、という
    protect_live の現実シナリオを再現する。reload しない provider なら新規ポジは見えず
    利益保護が欠落する (Codex High FIX 2)。
    """
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import Order, PositionManager

    sd = tmp_path
    _seed_balance(sd)
    prot_position_mgr = PositionManager(StateStore(sd), context="ProtectionWorker")

    # bootstrap が配線する provider と同一実装 (reload → read)。
    def provider():
        prot_position_mgr.reload()
        return prot_position_mgr.get_account_state().open_positions

    assert provider() == []  # daemon 起動直後はポジゼロ

    # daemon 起動「後」に別 PositionManager がポジションを建てて disk に書く。
    trade_mgr = PositionManager(StateStore(sd), context="TradeCycle")
    trade_mgr.open_position(
        Order.new(
            pair="EURUSD=X", direction="sell", entry_price=1.10,
            stop_loss=1.11, take_profit=1.08, position_size=10000.0,
            signal_reason="test",
        )
    )

    # provider は次の tick で新規ポジションを反映する (reload するため)。
    positions = provider()
    assert len(positions) == 1
    assert positions[0].pair == "EURUSD=X"


def test_runtime_starts_and_stops_protection_worker(monkeypatch, tmp_path: Path):
    started = {"prod": False, "worker": False}
    stopped = {"prod": False, "worker": False}

    class _StubProd:
        def start(self): started["prod"] = True
        def stop(self): stopped["prod"] = True
        def latest(self, pair): return None

    class _StubWorker:
        def start(self): started["worker"] = True
        def stop(self): stopped["worker"] = True

    rt = _make_minimal_runtime(
        tmp_path, quote_producer=_StubProd(), protection_worker=_StubWorker(),
    )
    rt.start()
    try:
        assert started["prod"] and started["worker"]
    finally:
        rt.stop()
    assert stopped["prod"] and stopped["worker"]
