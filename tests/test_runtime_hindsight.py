"""runtime の hindsight 配線テスト (plan Phase 4 Task 4.5)。

- trigger 確定時に pending hindsight 行を enqueue する (Plan C)。
- run_hindsight_cycle が horizon 経過 pending を評価し evaluated に更新する。
- shadow 境界: order_intents / broker には一切触れない。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.hindsight_evaluator import HindsightEvaluator
from src.orchestrator.runtime import OrchestratorRuntime
from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.utils.clock import db_now

# NOW は db_now() 相対 (全時刻系を揃え date-flake を避ける、review Medium)。
NOW = db_now()
FUTURE = NOW + timedelta(days=1)


def _seed_ok_technical(db: Path) -> None:
    # analyzed_at=NOW-60s は実 db_now lookback 窓内かつ age=60s で fresh (正の age で検証)。
    AnalysisStore(db).add_snapshot(
        TechnicalSnapshotData(
            pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.7,
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def _ohlcv_provider(df: pd.DataFrame):
    def provider(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if len(df) == 0:
            return df
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return provider


def _make_runtime(
    tmp_path: Path, *, mid: float, ohlcv: pd.DataFrame, horizon_seconds: int = 86400,
) -> tuple[OrchestratorRuntime, OrchestratorStore]:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)
    cfg = OrchestratorConfig()
    cfg.hindsight.horizon_seconds = horizon_seconds
    builder = DecisionContextBuilder(orch, AnalysisStore(db), cfg)

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - 0.005, ask=mid + 0.005, mid=mid, spread=0.01,
            source="test", observed_at=NOW,
        )

    runtime = OrchestratorRuntime(
        config=cfg,
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        hindsight_evaluator=HindsightEvaluator(ohlcv_provider=_ohlcv_provider(ohlcv)),
    )
    return runtime, orch


def _active_plan(orch: OrchestratorStore, *, entry: list[dict]) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=entry,
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=[], expires_at=FUTURE, created_by_run_id=run_id,
    )


def test_trigger_enqueues_pending_hindsight(tmp_path: Path) -> None:
    """trigger 確定時に pending hindsight 行が 1 件できる。"""
    runtime, orch = _make_runtime(tmp_path, mid=150.0, ohlcv=pd.DataFrame())
    plan_id = _active_plan(
        orch, entry=[{"type": "price_at_or_below", "value": 150.30}]
    )
    triggered = runtime.run_watch_cycle(now=NOW)
    assert triggered == [plan_id]

    trig = orch.get_shadow_trigger(plan_id)
    ev = orch.get_hindsight_evaluation(trig.id)
    assert ev is not None
    assert ev.status == "pending"
    assert ev.horizon_seconds == 86400
    # shadow 境界: 発注なし
    assert orch.get_order_intent(plan_id) is None


def test_hindsight_cycle_evaluates_past_horizon(tmp_path: Path) -> None:
    """horizon 経過後 run_hindsight_cycle が pending を評価し metric を埋める。"""
    # trigger 後に上昇して TP(151.5) 到達する OHLCV
    ohlcv = pd.DataFrame(
        {
            "Open": [150.0, 151.0], "High": [150.8, 151.6],
            "Low": [149.8, 150.9], "Close": [150.7, 151.5], "Volume": [0, 0],
        },
        index=pd.to_datetime([NOW + timedelta(hours=1), NOW + timedelta(hours=2)]),
    )
    runtime, orch = _make_runtime(tmp_path, mid=150.0, ohlcv=ohlcv)
    plan_id = _active_plan(
        orch, entry=[{"type": "price_at_or_below", "value": 150.30}]
    )
    runtime.run_watch_cycle(now=NOW)
    trig = orch.get_shadow_trigger(plan_id)

    # horizon 未経過: 評価されない
    assert runtime.run_hindsight_cycle(now=NOW + timedelta(hours=1)) == 0
    assert orch.get_hindsight_evaluation(trig.id).status == "pending"

    # horizon 経過: 評価される
    n = runtime.run_hindsight_cycle(now=NOW + timedelta(days=2))
    assert n == 1
    ev = orch.get_hindsight_evaluation(trig.id)
    assert ev.status == "evaluated"
    assert ev.would_hit_tp is True
    assert ev.pnl_r is not None
    assert ev.mfe_r is not None


def test_hindsight_cycle_marks_failed_when_no_ohlcv(tmp_path: Path) -> None:
    """horizon 内に OHLCV が無ければ status=failed に倒す (再評価ループを防ぐ)。"""
    runtime, orch = _make_runtime(tmp_path, mid=150.0, ohlcv=pd.DataFrame())
    plan_id = _active_plan(
        orch, entry=[{"type": "price_at_or_below", "value": 150.30}]
    )
    runtime.run_watch_cycle(now=NOW)
    trig = orch.get_shadow_trigger(plan_id)

    n = runtime.run_hindsight_cycle(now=NOW + timedelta(days=2))
    assert n == 1
    ev = orch.get_hindsight_evaluation(trig.id)
    assert ev.status == "failed"
    assert ev.mfe_r is None


def test_orphan_pending_is_marked_failed_not_relooped(tmp_path: Path) -> None:
    """shadow_trigger が見つからない pending 行は failed に倒し、再 query させない。

    放置すると pending のまま horizon 超過で毎 poll 再評価される無限ループになる。
    """
    runtime, orch = _make_runtime(tmp_path, mid=150.0, ohlcv=pd.DataFrame())
    # 実在しない shadow_trigger_id を指す孤児 pending を直接作る
    orch.record_hindsight_evaluation(shadow_trigger_id=99999, horizon_seconds=86400)

    n = runtime.run_hindsight_cycle(now=NOW + timedelta(days=2))
    assert n == 1
    ev = orch.get_hindsight_evaluation(99999)
    assert ev.status == "failed"
    # 2 回目は pending が無いので 0 件 (再ループしない)
    assert runtime.run_hindsight_cycle(now=NOW + timedelta(days=3)) == 0


def test_eval_exception_marks_failed_not_perpetual_pending(tmp_path: Path) -> None:
    """評価中に例外が出ても pending を残さず failed に倒す (毎 poll 再実行を防ぐ)。

    provider 例外 / カラム欠損 / tz 差分など deterministic な評価不能は、ログだけで
    pending のまま放置すると同じ行が毎 poll で永久に再実行される (Codex Medium)。
    """
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)
    cfg = OrchestratorConfig()
    builder = DecisionContextBuilder(orch, AnalysisStore(db), cfg)

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=149.995, ask=150.005, mid=150.0, spread=0.01,
            source="test", observed_at=NOW,
        )

    def boom_provider(symbol: str, start: datetime, end: datetime):
        raise RuntimeError("provider exploded")

    runtime = OrchestratorRuntime(
        config=cfg, orch_store=orch, context_builder=builder,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
        hindsight_evaluator=HindsightEvaluator(ohlcv_provider=boom_provider),
    )
    plan_id = _active_plan(
        orch, entry=[{"type": "price_at_or_below", "value": 150.30}]
    )
    runtime.run_watch_cycle(now=NOW)
    trig = orch.get_shadow_trigger(plan_id)

    n = runtime.run_hindsight_cycle(now=NOW + timedelta(days=2))
    assert n == 1
    ev = orch.get_hindsight_evaluation(trig.id)
    assert ev.status == "failed"
    # 2 回目は pending が無いので 0 件 (再ループしない)
    assert runtime.run_hindsight_cycle(now=NOW + timedelta(days=3)) == 0


def test_no_evaluator_does_not_enqueue(tmp_path: Path) -> None:
    """hindsight_evaluator 未注入なら trigger 時に enqueue しない (後方互換)。"""
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=149.995, ask=150.005, mid=150.0, spread=0.01,
            source="test", observed_at=NOW,
        )

    runtime = OrchestratorRuntime(
        config=OrchestratorConfig(), orch_store=orch, context_builder=builder,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
    )
    plan_id = _active_plan(
        orch, entry=[{"type": "price_at_or_below", "value": 150.30}]
    )
    runtime.run_watch_cycle(now=NOW)
    trig = orch.get_shadow_trigger(plan_id)
    assert orch.get_hindsight_evaluation(trig.id) is None
