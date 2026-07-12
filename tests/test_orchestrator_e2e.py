"""Orchestrator E2E 統合テスト (Phase 6 Task 6.4)。

material landing → planning (LLM pipeline) → active plan → watch trigger →
hindsight 評価 → shadow 通知 の一連を in-memory で駆動する。実 LLM / 実 broker は
使わず、scripted LLM + fake OHLCV provider + recording notifier で全段を貫通させる。

shadow 境界: order_intents / broker は一切登場しない。発注はしない。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.config.schema import AgentLlm, OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
from src.orchestrator.hindsight_evaluator import HindsightEvaluator
from src.orchestrator.material_landing import MaterialLandingDetector
from src.orchestrator.planner_agent import PlannerAgent
from src.orchestrator.planning_pipeline import PlanningPipeline
from src.orchestrator.risk_gate import RiskGateWorker
from src.orchestrator.runtime import OrchestratorRuntime
from src.utils.clock import db_now

# NOW は db_now() 相対 (全時刻系を揃え date-flake を避ける、review Medium)。
NOW = db_now()

OPP_YES = (
    '{"opportunity": "yes", "direction": "long", "score": 0.7, '
    '"confidence": 0.6, "reasoning_summary": "breakout setup"}'
)
FINAL_ACCEPT = (
    '{"decision": "accept", "final_score": 0.72, "confidence": 0.65, '
    '"reasoning_summary": "accepted"}'
)
DRAFT = (
    "{"
    '"direction": "long",'
    '"entry_conditions": [{"type": "price_at_or_below", "value": 150.30}],'
    '"action": {"sl": 149.40, "tp": 151.50, "size_policy": "risk", "rr": 2.0, "comment": "pullback"},'
    '"invalidation": [{"type": "price_below", "value": 148.0}],'
    '"expires_at": "2026-12-31T18:00:00",'
    '"reasoning_summary": "pullback long"'
    "}"
)


class _ScriptedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def chat(self, messages, temperature=0.1) -> str:
        return self._responses.pop(0)


class RecordingShadowNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def notify_plan_created(self, info) -> None:
        self.events.append(("plan_created", info))

    async def notify_plan_rejected(self, *, pair, reason) -> None:
        self.events.append(("plan_rejected", (pair, reason)))

    async def notify_plan_superseded(self, *, pair, old_plan_id, new_plan_id) -> None:
        self.events.append(("plan_superseded", (pair, old_plan_id, new_plan_id)))

    async def notify_shadow_trigger(self, info) -> None:
        self.events.append(("shadow_trigger", info))

    async def notify_hindsight_evaluated(self, info) -> None:
        self.events.append(("hindsight", info))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


def _seed_ok_technical(db: Path, *, bias: float = 0.5) -> None:
    # NOW が db_now() 相対なので analyzed_at=NOW-60s は実 db_now lookback 窓内かつ
    # age=60s で fresh (正の age で検証、review Medium)。
    AnalysisStore(db).add_snapshot(
        TechnicalSnapshotData(
            pair="USDJPY=X", direction_bias="long", bias_score=bias, confidence=0.7,
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def _fake_ohlcv_provider(symbol, start, end):
    """trigger 後 TP (151.50) に到達する 1h バー列を返す (列名は load_ohlcv 規約=Capitalized)。

    バー時刻は trigger (start) 起点にする (hindsight は triggered_at 以降の窓を引くため、
    バーが窓内に入る必要がある)。
    """
    idx = pd.date_range(start=start, periods=6, freq="1h")
    return pd.DataFrame(
        {
            "Open": [150.1, 150.4, 150.8, 151.2, 151.5, 151.4],
            "High": [150.5, 150.9, 151.3, 151.6, 151.7, 151.5],
            "Low": [150.0, 150.3, 150.7, 151.0, 151.3, 151.2],
            "Close": [150.4, 150.8, 151.2, 151.5, 151.5, 151.4],
            "Volume": [0, 0, 0, 0, 0, 0],
        },
        index=idx,
    )


class _Clock:
    """quote observed_at を「現在時刻」に追従させるためのテスト用 holder。

    実運用では quote は live (常に最新) なので observed_at は評価時刻に近い。固定 NOW を
    返すと watch 時に quote_age が max_quote_age_seconds(10s) を超え freshness block に
    なるため、テストでも cycle 時刻に合わせて更新する。
    """

    def __init__(self, t: datetime) -> None:
        self.now = t


def _build_runtime(
    tmp_path: Path, notifier=None, clock: "_Clock | None" = None
) -> tuple[OrchestratorRuntime, OrchestratorStore]:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    clock = clock or _Clock(NOW)

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.095, ask=150.105, mid=150.10, spread=0.01,
            source="test", observed_at=clock.now,
        )

    llm = _ScriptedLLM([OPP_YES, DRAFT, FINAL_ACCEPT])
    bundle = AgentLlm(client=llm, temperature=0.1)
    pipeline = PlanningPipeline(
        orch_store=orch,
        planner=PlannerAgent(bundle),
        execution_agent=ExecutionOpinionAgent(bundle),
        risk_gate=RiskGateWorker(min_rr=1.5, spread_max_pips=2.0, pip_size=0.01),
        config=OrchestratorConfig(),
    )

    # detector の technical 入力は wall-clock 非依存の固定スナップで与える。
    # get_recent_ok_snapshots は db_now() 相対 lookback のため、固定 NOW(2026-06-21) seed が
    # 実時刻と離れると窓外になり material 判定が時刻依存で揺れる。E2E は debounce 経路を
    # 安定検証したいので material=True を固定する (collect_status=ok / bias 固定)。
    class _FakeTech:
        collect_status = "ok"
        direction_bias = "long"
        bias_score = 0.5

    detector = MaterialLandingDetector(
        get_latest_technical=lambda pair: _FakeTech(),
        material_bias_delta_min=0.20,
        pairs=["USDJPY=X"],
    )
    rt = OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        detector=detector,
        pipeline=pipeline,
        hindsight_evaluator=HindsightEvaluator(ohlcv_provider=_fake_ohlcv_provider),
        shadow_notifier=notifier,
    )
    return rt, orch


def test_e2e_landing_to_planning_to_trigger_to_hindsight_to_notify(
    tmp_path: Path,
) -> None:
    notifier = RecordingShadowNotifier()
    clock = _Clock(NOW)
    rt, orch = _build_runtime(tmp_path, notifier=notifier, clock=clock)

    # ① planning: material landing。debounce 窓 (既定 180s) のため 2 周必要。
    #    1 周目で窓が開き (fire しない)、debounce 経過後の 2 周目で pipeline が走る (§5.4)。
    rt.run_planning_cycle(now=NOW)
    assert orch.get_active_plans("USDJPY=X") == [], "debounce 1 周目は発火しない"
    after_debounce = NOW + timedelta(seconds=200)
    clock.now = after_debounce
    rt.run_planning_cycle(now=after_debounce)
    active = orch.get_active_plans("USDJPY=X")
    assert len(active) == 1, "debounce 後に active plan が 1 件作られる"
    plan_id = active[0].plan_id
    assert "plan_created" in notifier.kinds()

    # ② watch: entry 成立 → shadow trigger + pending hindsight enqueue + 通知
    watch_at = after_debounce + timedelta(seconds=5)
    clock.now = watch_at
    triggered = rt.run_watch_cycle(now=watch_at)
    assert triggered == [plan_id]
    assert orch.get_trade_plan(plan_id).status == "triggered"
    assert "shadow_trigger" in notifier.kinds()
    trig = orch.get_shadow_trigger(plan_id)
    assert trig is not None and trig.tp == 151.50

    # ③ hindsight: horizon 経過後に評価 → TP 到達で pnl_r>0 + 通知
    later = watch_at + timedelta(days=2)
    evaluated = rt.run_hindsight_cycle(now=later)
    assert evaluated == 1
    assert "hindsight" in notifier.kinds()
    _, hs = next(e for e in notifier.events if e[0] == "hindsight")
    assert hs.would_hit_tp is True
    assert hs.pnl_r is not None and hs.pnl_r > 0

    # shadow 境界: order_intents は 1 件も無い (発注していない)。
    assert orch.get_order_intent(plan_id) is None


def test_e2e_no_order_intents_anywhere(tmp_path: Path) -> None:
    """E2E 全段を通しても order_intents テーブルに行が入らない (shadow 境界)。"""
    clock = _Clock(NOW)
    rt, orch = _build_runtime(tmp_path, clock=clock)
    rt.run_planning_cycle(now=NOW)  # debounce 窓を開く
    after = NOW + timedelta(seconds=200)
    clock.now = after
    rt.run_planning_cycle(now=after)  # debounce 後に plan 作成
    plan_id = orch.get_active_plans("USDJPY=X")[0].plan_id
    watch_at = after + timedelta(seconds=5)
    clock.now = watch_at
    rt.run_watch_cycle(now=watch_at)
    rt.run_hindsight_cycle(now=watch_at + timedelta(days=2))
    assert orch.get_order_intent(plan_id) is None
