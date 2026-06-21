"""MarketStateDetector + MarketStateBridge (Phase1 Task C, §5.2 / §5.2.1)。

(既存 tests/test_market_state.py は market-hours の market_skip_check 用で別物。本ファイルは
orchestrator の volatility/spread ベース state 検知器を対象とする。)

- 上げ即時 / 下げは安定継続 (ヒステリシス)。
- critical 条件 (spread spike / bridge / position 近接)。
- horizon overlay (swing は鈍く / day は敏感に)。
- bridge: active/critical で state boost、calm で取消、regime 上昇でコールバック。
now 注入で決定論。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.config.schema import OrchestratorMarketStateConfig
from src.orchestrator.cadence_resolver import CadenceResolver
from src.orchestrator.cadence_sources import MarketStateBridge
from src.orchestrator.market_state_detector import (
    ACTIVE, CALM, CRITICAL, NORMAL, MarketObservation,
    MarketStateDetector, detector_with_horizon,
)

NOW = datetime(2026, 6, 21, 12, 0, 0)


def _cfg(**kw):
    c = OrchestratorMarketStateConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── 上げ即時 / 下げ猶予 ───────────────────────────────────────

def test_upgrade_to_active_is_immediate():
    det = MarketStateDetector(_cfg(active_move_pct=0.15))
    s = det.observe("USDJPY=X", MarketObservation(move_pct=0.2), NOW)
    assert s == ACTIVE


def test_downgrade_requires_stability():
    det = MarketStateDetector(_cfg(active_move_pct=0.15, normal_after_seconds=120))
    det.observe("USDJPY=X", MarketObservation(move_pct=0.2), NOW)  # active
    # 静穏化しても猶予前は active のまま (バタつき防止)。
    assert det.observe("USDJPY=X", MarketObservation(move_pct=0.0),
                       NOW + timedelta(seconds=60)) == ACTIVE
    # 猶予を満たすと normal へ落ちる。
    assert det.observe("USDJPY=X", MarketObservation(move_pct=0.0),
                       NOW + timedelta(seconds=200)) == NORMAL


def test_normal_to_calm_after_long_stability():
    det = MarketStateDetector(_cfg(calm_after_seconds=600))
    det.observe("USDJPY=X", MarketObservation(move_pct=0.0), NOW)
    assert det.observe("USDJPY=X", MarketObservation(move_pct=0.0),
                       NOW + timedelta(seconds=601)) == CALM


def test_re_spike_resets_downgrade():
    det = MarketStateDetector(_cfg(active_move_pct=0.15, normal_after_seconds=120))
    det.observe("USDJPY=X", MarketObservation(move_pct=0.2), NOW)  # active
    det.observe("USDJPY=X", MarketObservation(move_pct=0.0), NOW + timedelta(seconds=60))
    assert det.observe("USDJPY=X", MarketObservation(move_pct=0.3),
                       NOW + timedelta(seconds=70)) == ACTIVE


# ── critical 条件 ─────────────────────────────────────────────

def test_spread_spike_critical():
    det = MarketStateDetector(_cfg(spread_spike_pips=4.0))
    assert det.observe("USDJPY=X", MarketObservation(spread_pips=5.0), NOW) == CRITICAL


def test_bridge_degraded_critical():
    det = MarketStateDetector(_cfg())
    assert det.observe("USDJPY=X", MarketObservation(bridge_degraded=True), NOW) == CRITICAL


def test_position_near_sl_tp_critical():
    det = MarketStateDetector(_cfg())
    obs = MarketObservation(has_position=True, position_near_sl_tp=True)
    assert det.observe("USDJPY=X", obs, NOW) == CRITICAL


def test_unknown_spread_not_critical():
    det = MarketStateDetector(_cfg(spread_spike_pips=4.0))
    # spread None (不明) は critical にしない (move_pct も低いので normal)。
    assert det.observe("USDJPY=X", MarketObservation(spread_pips=None), NOW) == NORMAL


# ── horizon overlay ──────────────────────────────────────────

def test_horizon_swing_is_less_sensitive():
    base = _cfg(active_move_pct=0.10)
    swing = detector_with_horizon(base, "swing")  # 閾値 0.20
    day = detector_with_horizon(base, "day")       # 閾値 0.05
    assert swing.observe("USDJPY=X", MarketObservation(move_pct=0.12), NOW) == NORMAL
    assert day.observe("USDJPY=X", MarketObservation(move_pct=0.12), NOW) == ACTIVE


# ── MarketStateBridge (C-2/C-3) ──────────────────────────────

def _resolver():
    return CadenceResolver(
        trade_pairs=["USDJPY=X"], watch_pairs=[],
        trade_base_interval_sec=3600, watch_base_interval_sec=7200,
    )


def test_bridge_boosts_on_active_and_clears_on_calm():
    r = _resolver()
    bridge = MarketStateBridge(resolver=r, boost_interval_sec=300, boost_ttl_sec=600)
    bridge.update("USDJPY=X", ACTIVE, NOW)
    assert r.effective_interval("USDJPY=X", NOW) == 300
    bridge.update("USDJPY=X", CALM, NOW + timedelta(seconds=10))
    assert r.effective_interval("USDJPY=X", NOW + timedelta(seconds=10)) == 3600


def test_bridge_fires_regime_on_upgrade_only():
    r = _resolver()
    events = []
    bridge = MarketStateBridge(
        resolver=r, boost_interval_sec=300, boost_ttl_sec=600,
        on_regime_change=lambda pair, st: events.append((pair, st)),
    )
    bridge.update("USDJPY=X", ACTIVE, NOW)
    bridge.update("USDJPY=X", CRITICAL, NOW)
    bridge.update("USDJPY=X", NORMAL, NOW)
    assert events == [("USDJPY=X", ACTIVE), ("USDJPY=X", CRITICAL)]


# ── runtime 配線 (C-4) ───────────────────────────────────────

def test_runtime_market_state_cycle_drives_detector(tmp_path):
    """run_market_state_cycle が quote から move_pct を出し detector を回す。"""
    from src.config.schema import OrchestratorConfig
    from src.data.analysis_store import AnalysisStore
    from src.data.orchestrator_store import OrchestratorStore
    from src.orchestrator.context_builder import (
        DecisionContextBuilder, QuoteSnapshot,
    )
    from src.orchestrator.runtime import OrchestratorRuntime

    db = tmp_path / "orch.db"
    cfg = OrchestratorConfig(enabled=True)
    cfg.market_state.active_move_pct = 0.15
    orch = OrchestratorStore(db)
    ctx = DecisionContextBuilder(orch, AnalysisStore(db), cfg)

    # active_move_pct=0.15(%)。2 回目 mid は +0.67% 変動 → 閾値超で ACTIVE。
    mids = iter([150.0, 151.0])

    def quote_provider(pair):
        return QuoteSnapshot(bid=0, ask=0, mid=next(mids), spread=None,
                             source="t", observed_at=NOW)

    det = MarketStateDetector(cfg.market_state)
    rt = OrchestratorRuntime(
        config=cfg, orch_store=orch, context_builder=ctx,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
        market_state_detector=det,
    )
    rt.run_market_state_cycle(NOW)  # 初回 (mid baseline, move=0 → 即時 active にはならない)
    out = rt.run_market_state_cycle(NOW + timedelta(seconds=30))
    # 0.67% > 0.15% で ACTIVE に上がる (単位整合の回帰防止)。
    assert out["USDJPY=X"] == ACTIVE


def test_runtime_no_detector_is_noop(tmp_path):
    from src.config.schema import OrchestratorConfig
    from src.data.analysis_store import AnalysisStore
    from src.data.orchestrator_store import OrchestratorStore
    from src.orchestrator.context_builder import (
        DecisionContextBuilder, QuoteSnapshot,
    )
    from src.orchestrator.runtime import OrchestratorRuntime

    db = tmp_path / "orch.db"
    cfg = OrchestratorConfig(enabled=True)
    orch = OrchestratorStore(db)
    ctx = DecisionContextBuilder(orch, AnalysisStore(db), cfg)
    rt = OrchestratorRuntime(
        config=cfg, orch_store=orch, context_builder=ctx,
        pairs=["USDJPY=X"],
        quote_provider=lambda p: QuoteSnapshot(
            bid=0, ask=0, mid=150.0, spread=None, source="t", observed_at=NOW),
    )
    # detector 未注入なら空 dict (no-op)。
    assert rt.run_market_state_cycle(NOW) == {}
