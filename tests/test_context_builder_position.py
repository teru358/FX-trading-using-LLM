"""position ブロック整形 (P-1): raw dict → 正規化 + pnl_r 算出 + fail-safe。"""
from datetime import datetime

import pytest

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot


def _builder(tmp_path, position_provider=None):
    db = tmp_path / "o.db"
    return DecisionContextBuilder(
        OrchestratorStore(db), AnalysisStore(db), OrchestratorConfig(),
        position_provider=position_provider,
    )


def _quote():
    return QuoteSnapshot(bid=151.0, ask=151.02, mid=151.01, spread=0.02,
                         source="test", observed_at=datetime(2026, 7, 5, 12, 0))


def test_no_provider_yields_empty_block(tmp_path):
    ctx = _builder(tmp_path).build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0),
                                   quote=_quote())
    assert ctx["position"] == {"count": 0, "items": []}


def test_position_shaped_with_pnl_r(tmp_path):
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 10000,
            "opened_at": "2026-07-05T09:00:00", "mfe_r": 1.5,
            "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": "breakout"}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    item = ctx["position"]["items"][0]
    assert ctx["position"]["count"] == 1
    assert item["direction"] == "long"              # buy → long 正規化
    assert item["pnl_r"] == pytest.approx((151.01 - 150.0) / 0.5, abs=0.01)
    assert item["mfe_r"] == 1.5


def test_sell_position_pnl_r_sign_flipped(tmp_path):
    raw = [{"direction": "sell", "entry_price": 152.0, "size": 10000,
            "opened_at": None, "mfe_r": 0.0,
            "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"]["items"][0]["direction"] == "short"
    assert ctx["position"]["items"][0]["pnl_r"] == pytest.approx((152.0 - 151.01) / 0.5, abs=0.01)


def test_zero_risk_distance_gives_null_pnl_r(tmp_path):
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 1,
            "opened_at": None, "mfe_r": 0.0, "initial_risk_price_distance": 0.0,
            "is_scale_in": False, "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"]["items"][0]["pnl_r"] is None


def test_provider_exception_yields_unavailable(tmp_path):
    def boom(pair):
        raise RuntimeError("state file corrupt")
    ctx = _builder(tmp_path, boom).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"] == {"count": None, "items": [], "status": "unavailable"}


def test_assemble_keeps_stub_position(tmp_path):
    """watch tick 経路 (assemble) は provider 注入済みでも stub のまま (設計:
    1s tick で position reload しない)。"""
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 10000,
            "opened_at": None, "mfe_r": 0.0, "initial_risk_price_distance": 0.5,
            "is_scale_in": False, "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).assemble(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"] == {"count": 0, "items": []}


def test_malformed_raw_position_degrades_to_unavailable(tmp_path):
    """整形 loop 中の例外 (壊れた raw dict) も unavailable に倒す (Task 5 Minor#1)。"""
    raw = [{"direction": "buy", "entry_price": "not-a-number", "size": 1,
            "opened_at": None, "mfe_r": 0.0, "initial_risk_price_distance": 0.5,
            "is_scale_in": False, "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"] == {"count": None, "items": [], "status": "unavailable"}


# ── current_plan ブロック (P-2) ─────────────────────────────


def test_current_plan_block_from_active_plan(tmp_path):
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    sid = orch.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 11, 0))
    orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=sid, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 149.8}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    cur = ctx["current_plan"]
    assert cur["direction"] == "long"
    assert cur["status"] == "active"
    assert "price_at_or_below" in cur["entry_summary"]


def test_current_plan_entry_summary_non_price_conditions(tmp_path):
    """type ごとの参照キー出し分け (codex Medium): value / value_pips / status。"""
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    sid = orch.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 11, 0))
    orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=sid, horizon="day", direction="long",
        entry_conditions_json=[
            {"type": "spread_below", "value_pips": 2.0},
            {"type": "technical_status_is", "status": "ok"},
        ],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert "spread_below 2.0" in ctx["current_plan"]["entry_summary"]
    assert "technical_status_is ok" in ctx["current_plan"]["entry_summary"]


def test_current_plan_none_when_no_active(tmp_path):
    ctx = _builder(tmp_path).build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0),
                                   quote=_quote())
    assert ctx["current_plan"] is None


def test_snapshot_stores_position_and_current_plan(tmp_path):
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 1, "opened_at": None,
            "mfe_r": 0.0, "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": ""}]
    builder = DecisionContextBuilder(
        orch, AnalysisStore(db), OrchestratorConfig(), position_provider=lambda p: raw)
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    snap = orch.get_snapshot(ctx["snapshot_id"])
    assert snap.position_json["count"] == 1
    assert snap.current_plan_json is None


def test_assemble_has_no_current_plan_key_or_none(tmp_path):
    """watch tick 経路 (assemble) は current_plan も stub (None) のまま (設計:
    1s tick で plan reload しない — position stub と同じ思想)。"""
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    sid = orch.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 11, 0))
    orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=sid, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 149.8}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    ctx = builder.assemble(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0),
                           quote=_quote())
    assert ctx.get("current_plan") is None
