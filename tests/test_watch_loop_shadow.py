"""watch loop の shadow trigger flow テスト (design §7)。

run_watch_cycle が active plan を評価し、entry 成立時に plan_trigger を shadow 記録
する。expiry / invalidation / freshness block は trigger せず、broker / order_intents
には一切触れない (§7.3 shadow boundary)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime
from src.utils.clock import db_now

# NOW は注入する評価時刻。quote age / plan expiry / technical age の共通基準。
# **実 db_now() 相対**にして全時刻系 (lookback / age / expiry / quote) を揃える
# (固定過去日付だと get_recent_ok_snapshots の db_now lookback と乖離し date-flake になる、
# review Medium)。db_now() は import 時 1 回評価 = テスト内では決定論的。
NOW = db_now()
FUTURE = NOW + timedelta(days=1)
PAST = NOW - timedelta(hours=1)


def _seed_ok_technical(db: Path, *, analyzed_at: datetime | None = None) -> None:
    """freshness final wall を通すため ok technical snapshot を 1 件入れる。

    NOW が db_now() 相対なので、analyzed_at=NOW-60s は (a) get_recent_ok_snapshots の
    実 db_now lookback (24h) 窓内、かつ (b) build(now=NOW) の age=60s が max_stale 内。
    負の age に依存せず **正の age=60s で fresh** を検証するため freshness 回帰として健全
    (review Medium 対応)。
    """
    if analyzed_at is None:
        analyzed_at = NOW - timedelta(seconds=60)
    AnalysisStore(db).add_snapshot(
        TechnicalSnapshotData(
            pair="USDJPY=X",
            direction_bias="long",
            bias_score=0.5,
            confidence=0.7,
            analyzed_at=analyzed_at,
        )
    )


def _make_runtime(
    tmp_path: Path, *, mid: float, spread: float = 0.01, seed_technical: bool = False,
) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    if seed_technical:
        _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - spread / 2, ask=mid + spread / 2, mid=mid, spread=spread,
            source="test", observed_at=NOW,
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
    )


def _create_plan(
    orch: OrchestratorStore,
    *,
    entry: list[dict],
    invalidation: list[dict] | None = None,
    expires_at: datetime = FUTURE,
    direction: str = "long",
) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction=direction,
        entry_conditions_json=entry,
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=invalidation or [],
        expires_at=expires_at, created_by_run_id=run_id,
    )


def test_entry_hold_records_shadow_trigger(tmp_path: Path) -> None:
    """entry 成立 → plan_trigger decision + shadow_trigger 記録 + plan=triggered。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == [plan_id]
    # plan が triggered になった
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "triggered"
    # shadow_trigger が残った
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.pair == "USDJPY=X"
    assert trig.direction == "long"
    assert trig.trigger_price == 150.10
    # plan_trigger decision が残り、shadow_trigger と紐づく
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec is not None
    assert dec.decision_type == "plan_trigger"
    assert dec.plan_id == plan_id
    # trigger 時点の snapshot は planning 時点と別 (新規 materialize, §7.1)
    assert dec.snapshot_id != plan.snapshot_id
    assert trig.snapshot_id == dec.snapshot_id


def test_entry_not_hold_no_trigger(tmp_path: Path) -> None:
    """entry 未成立なら trigger しない。"""
    rt = _make_runtime(tmp_path, mid=150.50)  # above the at_or_below threshold
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    assert rt._orch.get_trade_plan(plan_id).status == "active"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_expired_plan_is_invalidated_not_triggered(tmp_path: Path) -> None:
    """expires_at を過ぎた plan は expired にし trigger しない。"""
    rt = _make_runtime(tmp_path, mid=150.10)
    plan_id = _create_plan(
        rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}],
        expires_at=PAST,
    )

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    assert rt._orch.get_trade_plan(plan_id).status == "expired"
    assert rt._orch.get_shadow_trigger(plan_id) is None
    # plan_invalidate decision が残る
    dec = rt._orch.get_decision(1)
    assert dec is not None
    assert dec.decision_type == "plan_invalidate"


def test_invalidation_breached_marks_invalidated(tmp_path: Path) -> None:
    """invalidation 成立 (price_below) で invalidated にし trigger しない。"""
    rt = _make_runtime(tmp_path, mid=148.50)
    plan_id = _create_plan(
        rt._orch,
        entry=[{"type": "price_at_or_below", "value": 150.30}],
        invalidation=[{"type": "price_below", "value": 149.0}],
    )

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_freshness_block_prevents_trigger(tmp_path: Path) -> None:
    """freshness final wall 失敗 (wide spread) なら entry 成立でも trigger しない。"""
    # spread 0.05 = 5 pips > spread_max_pips 2.0 -> freshness block
    rt = _make_runtime(tmp_path, mid=150.10, spread=0.05)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "active"   # 失効はさせない (§7.2)
    assert rt._orch.get_shadow_trigger(plan_id) is None
    # freshness 行に issue が残る (block 時は plan の既存 snapshot に記録する。
    # 新規 snapshot は trigger 確定時のみ materialize する — §7.1)。
    fresh = rt._orch.get_freshness_for_snapshot(plan.snapshot_id)
    assert fresh is not None
    assert fresh.issues_json  # non-empty issues


def test_shadow_boundary_no_order_intents(tmp_path: Path) -> None:
    """trigger しても order_intents は 0 件のまま (§7.3 shadow boundary)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == [plan_id]   # 前提: 実際に trigger した上で order_intents 0 を確認

    # broker / order path に触れていない: order_intent が作られていない
    assert rt._orch.get_order_intent(plan_id) is None


def test_triggered_plan_not_retriggered_next_cycle(tmp_path: Path) -> None:
    """一度 triggered になった plan は active から外れ、次 cycle で再 trigger しない。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    first = rt.run_watch_cycle(now=NOW)
    second = rt.run_watch_cycle(now=NOW + timedelta(seconds=1))

    assert first == [plan_id]
    assert second == []  # active でなくなったので拾われない
    # shadow_trigger は 1 件のまま (重複記録しない)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None


def test_trigger_failure_marks_run_failed_not_dangling(tmp_path: Path) -> None:
    """trigger 経路で build() 等が落ちても agent_run は failed で finish される。

    start_run 後・finish_run 前の例外で run が status=ok・finished_at=None のまま
    残らないこと (planning loop と同じ try/finally 規律)。
    """
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_plan(rt._orch, entry=[{"type": "price_at_or_below", "value": 150.30}])

    # trigger 確定後に build() が落ちる状況を作る (新規 snapshot materialize 失敗)。
    real_build = rt._ctx.build

    def boom_build(*args, **kwargs):
        raise RuntimeError("snapshot write failed")

    rt._ctx.build = boom_build  # type: ignore[method-assign]
    triggered = rt.run_watch_cycle(now=NOW)
    rt._ctx.build = real_build  # type: ignore[method-assign]

    assert triggered == []  # 落ちたので trigger 成功扱いにしない
    # この watch run は failed で finish され、dangling しない
    runs = [rt._orch.get_run(i) for i in range(1, 10)]
    watch_runs = [r for r in runs if r is not None and r.trigger_type == "watch_cycle"]
    assert watch_runs, "watch run should have been started"
    for r in watch_runs:
        assert r.finished_at is not None
        assert r.status == "failed"
    # plan は triggered にならない (失敗時に状態を進めない)
    assert rt._orch.get_trade_plan(plan_id).status == "active"


def test_unparseable_entry_condition_invalidates_plan(tmp_path: Path) -> None:
    """壊れた entry_condition (未知 type) を持つ plan は毎 tick 例外ループにせず失効させる。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    snap = rt._orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = rt._orch.start_run("PlannerAgent", pair="USDJPY=X")
    plan_id = rt._orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "bogus_unknown_type", "value": 1.0}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=FUTURE, created_by_run_id=run_id,
    )

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    # active のまま残して毎秒例外を吐かせない: 評価不能な plan は失効させる
    assert rt._orch.get_trade_plan(plan_id).status != "active"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_news_conflict_invalidation_fires_for_long_plan(tmp_path: Path) -> None:
    """強い negative news が long plan の news_conflict invalidation を発火させる。

    HIGH fix: assemble() の context に news_conflict が無いと永久に発火しないため、
    runtime が plan.direction を見て ctx["news_conflict"] を算出することを検証する。
    """
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)

    def news_provider(pair: str) -> dict:
        # 強い negative (long に逆行)。entry.news_impact_min=0.5 を超える。
        return {"sentiment_score": -0.8, "confidence": 0.9, "top_reasons": ["risk-off"]}

    builder = DecisionContextBuilder(
        orch, AnalysisStore(db), OrchestratorConfig(), news_provider=news_provider
    )

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.09, ask=150.11, mid=150.10, spread=0.01, source="test", observed_at=NOW,
        )

    rt = OrchestratorRuntime(
        config=OrchestratorConfig(), orch_store=orch, context_builder=builder,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
    )
    plan_id = _create_plan(
        rt._orch,
        entry=[{"type": "price_at_or_below", "value": 150.30}],   # entry would otherwise hold
        invalidation=[{"type": "news_conflict"}],
        direction="long",
    )

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []
    # news_conflict が発火し invalidated になる (trigger より優先)
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_plan_with_no_expiry_does_not_crash_trigger(tmp_path: Path) -> None:
    """expires_at=None の plan でも shadow risk pre-check が落ちず trigger できる。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    snap = rt._orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = rt._orch.start_run("PlannerAgent", pair="USDJPY=X")
    plan_id = rt._orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=None, created_by_run_id=run_id,
    )

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == [plan_id]
    assert rt._orch.get_trade_plan(plan_id).status == "triggered"
