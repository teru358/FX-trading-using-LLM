"""OrchestratorRuntime (spec §4) — 非LLM ランタイム。

Phase 1 foundation: planning loop と watch loop の 2 ループの骨格。
- planning loop: Context Builder で decision_snapshot を materialize し、
  direct_hold decision を記録する (PlannerAgent / plan 作成は later plan)。
- watch loop: active plan を走査し freshness を記録する。**発注は行わない**
  (entry_conditions の実評価・執行は later plan)。

両ループは orchestrator.enabled が true のときのみ start() で起動する。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from src.config.schema import OrchestratorConfig
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.hindsight_evaluator import HindsightEvaluator
from src.orchestrator.risk_gate import RiskGateWorker
from src.orchestrator.schemas import (
    EntryCondition,
    ExecutionPlanDraft,
    InvalidationCondition,
    SchemaParseError,
)
from src.orchestrator.watch_evaluator import WatchEvaluator
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.orchestrator.material_landing import MaterialLandingDetector
    from src.orchestrator.planning_pipeline import PlanningPipeline

logger = logging.getLogger(__name__)

QuoteProvider = Callable[[str], QuoteSnapshot]


def _side_of(direction: str | None) -> str | None:
    """plan direction (long/short) を decision.decision の side (buy/sell) に写像する。"""
    if direction == "long":
        return "buy"
    if direction == "short":
        return "sell"
    return None


class OrchestratorRuntime:
    """2 ループ (planning / watch) を駆動する非LLM ランタイム骨格。"""

    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        orch_store: OrchestratorStore,
        context_builder: DecisionContextBuilder,
        pairs: list[str],
        quote_provider: QuoteProvider,
        detector: "MaterialLandingDetector | None" = None,
        pipeline: "PlanningPipeline | None" = None,
        evaluator: WatchEvaluator | None = None,
        risk_gate: RiskGateWorker | None = None,
        hindsight_evaluator: HindsightEvaluator | None = None,
    ) -> None:
        self._config = config
        self._orch = orch_store
        self._ctx = context_builder
        self._pairs = pairs
        self._quote_provider = quote_provider
        self._detector = detector
        self._pipeline = pipeline
        # trigger 後の判断品質を後追い計測する評価器 (Phase 4)。未注入なら hindsight を
        # enqueue/評価しない (Phase 1〜3 後方互換・shadow 境界も不変)。
        self._hindsight = hindsight_evaluator
        # watch loop の条件評価層。未注入なら config.entry から既定構築する。
        self._evaluator = evaluator or WatchEvaluator(config.entry)
        # trigger 直前の shadow risk pre-check (§7.1)。hard veto ではなく結果を
        # shadow_triggers に残すだけ — Phase 2 は発注しないため pre_check の reject でも
        # trigger 記録は行う (判断品質データ収集が目的)。
        self._risk_gate = risk_gate or RiskGateWorker(
            spread_max_pips=config.entry.spread_max_pips
        )
        self._stop = threading.Event()
        self._planning_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._hindsight_thread: threading.Thread | None = None

    # ── 1 サイクル分の処理 (テスト・手動駆動可) ─────────────────

    def run_planning_cycle(self, now: datetime | None = None) -> None:
        """全 pair について planning を 1 周する。

        Phase 1: snapshot を materialize し direct_hold を記録するのみ。
        PlannerAgent による trade_plan 作成は later plan で差し込む。
        """
        now = now or db_now()
        if self._detector is not None:
            target_pairs = self._detector.pairs_to_plan(now)
        else:
            target_pairs = self._pairs  # 後方互換: detector 未注入時は全 pair
        for pair in target_pairs:
            # start_run は quote 取得・build より前に呼ぶ: 価格取得は落ちやすい入力なので、
            # 失敗時も failed run を必ず残し (dangling 防止)、かつ 1 ペアの失敗で残りペアを
            # 止めないため、quote 取得も含めて try 内に入れる。
            run_id = self._orch.start_run(
                "OrchestratorRuntime",
                pair=pair,
                trigger_type="planning_cycle",
                trade_horizon=self._config.policy.trade_horizon,
            )
            committed = False
            try:
                quote = self._quote_provider(pair)
                ctx = self._ctx.build(pair=pair, now=now, quote=quote)
                # snapshot を run に後付けで紐付け、agent_run → decision_snapshot の
                # trace graph (§8.1) を繋ぐ (start_run 時点では snapshot 未作成のため)。
                self._orch.attach_snapshot(run_id, ctx["snapshot_id"])
                # snapshot 作成まで到達 = 材料を読めた。ここを境に material baseline を消費する。
                committed = True
                if self._pipeline is not None:
                    # Layer2 planning (Task 2.11)。planning thread には event loop が無いので
                    # asyncio.run で同期境界をまたぐ (二重 loop にならない)。pipeline は内部で
                    # fail-safe し、decision/plan の記録も自身で行う (§5.2/§5.4)。
                    result = asyncio.run(
                        self._pipeline.run(pair=pair, context=ctx, run_id=run_id)
                    )
                    if result.outcome == "failed":
                        # 一時失敗 (LLM timeout / parse / circuit) で baseline を消費しない:
                        # committed を下ろし finally で mark_attempted 側に倒す (Codex High)。
                        # 同じ材料の再 planning を抑制せず、次 tick で再評価させる。
                        committed = False
                        # 原因を DB に残し、後から SchemaParseError/Timeout 等を区別できる
                        # ようにする (Codex Medium)。error_type は pipeline 由来 failed の標識。
                        self._orch.finish_run(
                            run_id, status="failed",
                            error_type="PipelineFailed", error_message=result.error,
                        )
                    else:
                        self._orch.finish_run(run_id, status="ok")
                else:
                    # 後方互換 (pipeline 未注入): 機会判断を行わず direct_hold を記録する。
                    self._orch.record_decision(
                        run_id=run_id,
                        snapshot_id=ctx["snapshot_id"],
                        pair=pair,
                        decision_type="direct_hold",
                        decision="hold",
                        reasoning_summary="phase1 observe: no planning agent wired yet",
                        trade_horizon=self._config.policy.trade_horizon,
                    )
                    self._orch.finish_run(run_id, status="ok")
            except Exception as exc:
                logger.exception(f"[ORCH] planning cycle failed for {pair}")
                self._orch.finish_run(
                    run_id, status="failed",
                    error_type=type(exc).__name__, error_message=str(exc),
                )
            finally:
                # detector 注入時のみ mark (Codex Medium#4)。
                # - snapshot 作成まで到達 (committed) → mark_committed: baseline を消費し
                #   floor 起点を進める (同じ材料で再発火しない)。
                # - snapshot 前に失敗 → mark_attempted: debounce 窓だけ閉じる。材料を読めて
                #   いないので baseline は未消費 (次 tick で再評価され、毎 tick リトライはしない)。
                if self._detector is not None:
                    if committed:
                        self._detector.mark_committed(pair, now)
                    else:
                        self._detector.mark_attempted(pair, now)

    def run_watch_cycle(self, now: datetime | None = None) -> list[int]:
        """active plan を走査し、shadow trigger flow を回す (design §7)。

        各 active plan について優先順:
          1. expiry / invalidation 成立 → plan を invalidated/expired にし plan_invalidate
             を記録 (trigger しない)。
          2. freshness final wall 失敗 → freshness に issue を残し trigger しない
             (plan は active のまま、§7.2)。
          3. entry_conditions 成立 → trigger 時点 snapshot を新規 materialize し、
             plan_trigger decision + shadow_trigger を記録、plan を triggered にする。

        **Phase 2 shadow boundary (§7.3):** broker / MT5 / order_intents には一切
        触れない。発注 path はこの関数に存在しない。

        Returns: trigger した plan_id のリスト。
        """
        now = now or db_now()
        triggered: list[int] = []
        for pair in self._pairs:
            for plan in self._orch.get_active_plans(pair):
                try:
                    if self._evaluate_plan(plan, pair, now):
                        triggered.append(plan.plan_id)
                except Exception:
                    # 1 plan の評価失敗で他 plan / 他 pair を止めない (planning loop と同思想)。
                    logger.exception(
                        f"[ORCH] watch eval failed for plan {plan.plan_id} ({pair})"
                    )
        return triggered

    def run_hindsight_cycle(self, now: datetime | None = None) -> int:
        """horizon を過ぎた pending hindsight を評価し metric を埋める (Plan C poll)。

        各 pending について shadow_trigger を引き、HindsightEvaluator で MFE-R/MAE-R/
        PnL-R を算出する。評価成功なら status=evaluated、OHLCV 不足等で評価不能なら
        status=failed に倒す (再評価ループを防ぐ)。**発注は行わない (shadow boundary)。**

        Returns: 評価 (evaluated/failed どちらも含む) した件数。
        """
        if self._hindsight is None:
            return 0
        now = now or db_now()
        evaluated = 0
        for ev in self._orch.get_pending_hindsight_evaluations(now=now):
            try:
                if self._evaluate_one_hindsight(ev, now):
                    evaluated += 1
            except Exception:
                logger.exception(
                    f"[ORCH] hindsight eval failed for trigger {ev.shadow_trigger_id}"
                )
        return evaluated

    def _evaluate_one_hindsight(self, ev, now: datetime) -> bool:
        """1 pending hindsight 行を評価し DB に書き戻す。評価を試みたら True。"""
        trig = self._orch.get_shadow_trigger_by_id(ev.shadow_trigger_id)
        if trig is None:
            logger.warning(
                f"[ORCH] hindsight: shadow_trigger {ev.shadow_trigger_id} missing"
            )
            return False
        result = self._hindsight.evaluate(
            pair=trig.pair,
            direction=trig.direction,
            trigger_price=trig.trigger_price,
            sl=trig.sl,
            tp=trig.tp,
            triggered_at=trig.triggered_at,
            horizon_seconds=ev.horizon_seconds or self._config.hindsight.horizon_seconds,
        )
        if not result.has_data:
            self._orch.update_hindsight_evaluation(
                ev.id, status="failed", evaluated_at=now,
                reasoning_summary=result.reasoning_summary,
            )
            return True
        self._orch.update_hindsight_evaluation(
            ev.id, status="evaluated", evaluated_at=now,
            mfe_r=result.mfe_r, mae_r=result.mae_r, pnl_r=result.pnl_r,
            would_hit_sl=result.would_hit_sl, would_hit_tp=result.would_hit_tp,
            reasoning_summary=result.reasoning_summary,
        )
        return True

    def _evaluate_plan(self, plan, pair: str, now: datetime) -> bool:
        """1 plan を評価する。trigger したら True。

        評価 context は非永続 (assemble) で組み、trigger 確定時のみ build() で
        新規 snapshot を materialize する (§7.1)。
        """
        quote = self._quote_provider(pair)
        ctx = self._ctx.assemble(pair=pair, now=now, quote=quote)
        self._enrich_ages(ctx, now)
        # news_conflict は plan.direction を知る runtime 側で算出して ctx に注入する
        # (evaluator の invalidation_reason は direction を持たないため)。
        ctx["news_conflict"] = self._news_conflicts(ctx, plan.direction)

        # 保存済み条件を parse。壊れた条件 (未知 type / 欠損) を持つ plan は trigger
        # できないので、毎 tick 例外ループにせず失効させる (Codex review #5)。
        try:
            entry_conds = [
                EntryCondition.from_dict(c) for c in (plan.entry_conditions_json or [])
            ]
            inval_conds = [
                InvalidationCondition.from_dict(c) for c in (plan.invalidation_json or [])
            ]
        except SchemaParseError as exc:
            logger.warning(
                f"[ORCH] plan {plan.plan_id} ({pair}) has unparseable conditions, "
                f"invalidating: {exc}"
            )
            self._invalidate_plan(plan, pair, ctx, "unparseable_conditions", now)
            return False

        # 1. expiry / invalidation を最優先 (期限切れ・前提崩壊した plan は trigger しない)。
        reason = self._evaluator.invalidation_reason(
            inval_conds, ctx, now=now, expires_at=plan.expires_at
        )
        if reason is not None:
            self._invalidate_plan(plan, pair, ctx, reason, now)
            return False

        # 2. entry 未成立なら何もしない (freshness 評価は trigger 候補のみで十分)。
        if not self._evaluator.entry_conditions_hold(entry_conds, ctx):
            return False

        # 3. freshness final wall。失敗時は trigger せず既存 snapshot に issue を残す。
        issues = self._evaluator.freshness_issues(ctx)
        if issues:
            self._orch.record_freshness(
                snapshot_id=plan.snapshot_id, pair=pair, issues=issues
            )
            return False

        # 4. trigger 確定。新規 snapshot を materialize し shadow trigger を記録する。
        # claim に負けた (既に非 active) 場合は False が返り、triggered には数えない。
        return self._record_shadow_trigger(plan, pair, quote, now)

    @staticmethod
    def _enrich_ages(ctx: dict, now: datetime) -> None:
        """評価 context に quote_age_sec / technical.age_sec を後付けする。

        assemble() は ISO 文字列の observed_at / last_ok_at しか持たないため、now 基準で
        age 秒を算出して evaluator が freshness wall を判定できるようにする。
        """
        observed = ctx.get("quote", {}).get("observed_at")
        if observed:
            try:
                ctx["quote_age_sec"] = (now - datetime.fromisoformat(observed)).total_seconds()
            except (TypeError, ValueError):
                ctx["quote_age_sec"] = None
        last_ok = ctx.get("technical", {}).get("last_ok_at")
        if last_ok:
            try:
                ctx["technical"]["age_sec"] = (
                    now - datetime.fromisoformat(last_ok)
                ).total_seconds()
            except (TypeError, ValueError):
                ctx["technical"]["age_sec"] = None

    def _news_conflicts(self, ctx: dict, direction: str | None) -> bool:
        """news sentiment が plan 方向に逆行し、かつ閾値を超えていれば True (§6.3)。

        long plan は強い negative sentiment、short plan は強い positive sentiment を
        conflict とみなす。閾値は entry.news_impact_min。sentiment 欠落時は conflict 無し。
        direction を持つ runtime 側で算出し ctx["news_conflict"] に注入する。
        """
        score = ctx.get("news", {}).get("sentiment_score")
        if score is None or direction is None:
            return False
        threshold = self._config.entry.news_impact_min
        if direction == "long":
            return score <= -threshold
        if direction == "short":
            return score >= threshold
        return False

    def _invalidate_plan(self, plan, pair: str, ctx: dict, reason: str, now: datetime) -> None:
        """plan を expired/invalidated にし plan_invalidate decision を記録する (§6.3)。

        start_run 後・finish_run 前の例外で run が dangling しないよう try/finally で
        必ず finish する (planning loop と同規律, Codex review #2)。
        """
        status = "expired" if reason == "expired" else "invalidated"
        # 古い watch 評価が、別経路で triggered/superseded になった plan を invalidated/
        # expired に巻き戻すのを防ぐ: active のときだけ原子的に claim し、勝ったときだけ
        # decision を残す (Codex High#2)。負けたら何もしない。
        if not self._orch.try_claim_plan_status(plan.plan_id, status, from_status="active"):
            logger.info(
                f"[ORCH] plan {plan.plan_id} ({pair}) no longer active — "
                f"{status} ({reason}) skipped"
            )
            return
        run_id = self._orch.start_run(
            "OrchestratorRuntime", pair=pair, trigger_type="watch_cycle",
        )
        ok = False
        try:
            # plan_invalidate decision は planning 時点 snapshot に紐付ける (trigger 時の
            # 新規 snapshot は作らない — 失効に判断品質データは不要)。
            self._orch.record_decision(
                run_id=run_id, snapshot_id=plan.snapshot_id, pair=pair,
                decision_type="plan_invalidate", plan_id=plan.plan_id,
                reasoning_summary=f"watch invalidate: {reason}",
            )
            ok = True
            logger.info(f"[ORCH] plan {plan.plan_id} ({pair}) {status}: {reason}")
        finally:
            self._orch.finish_run(run_id, status="ok" if ok else "failed")

    def _record_shadow_trigger(self, plan, pair: str, quote: QuoteSnapshot, now: datetime) -> bool:
        """trigger 時点 snapshot を作り plan_trigger + shadow_trigger を記録する (§7.1)。

        Returns True if a shadow trigger was recorded. TOCTOU 対策として、まず
        active→triggered の条件付き UPDATE で plan を「予約」し、勝ったときだけ記録する
        (二重 trigger / 二重 shadow_trigger 行を防ぐ, Codex review #3)。負けたら False。
        start_run 後の例外でも finish_run を保証する (try/finally, Codex review #2)。
        """
        # 1. plan を active から triggered へ条件付きで claim。負け (既に非 active) なら何もしない。
        if not self._orch.try_mark_plan_triggered(plan.plan_id):
            logger.info(
                f"[ORCH] plan {plan.plan_id} ({pair}) already non-active — trigger skipped"
            )
            return False

        run_id = self._orch.start_run(
            "OrchestratorRuntime", pair=pair, trigger_type="watch_cycle",
        )
        ok = False
        try:
            # 新規 snapshot を materialize (planning 時点とは別。trigger 時の quote/freshness/
            # risk pre-check を独立 trace する — §7.1)。
            trigger_ctx = self._ctx.build(pair=pair, now=now, quote=quote)
            snapshot_id = trigger_ctx["snapshot_id"]
            self._orch.attach_snapshot(run_id, snapshot_id)

            # shadow risk pre-check。Phase 2 は hard veto にせず結果を残すだけ。
            action = plan.action_json or {}
            risk_result = self._shadow_risk_precheck(plan, trigger_ctx)

            decision_id = self._orch.record_decision(
                run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                decision_type="plan_trigger", decision=_side_of(plan.direction),
                plan_id=plan.plan_id, reasoning_summary="watch shadow trigger",
                risk_gate_result=risk_result, trade_horizon=plan.horizon,
            )
            shadow_trigger_id = self._orch.record_shadow_trigger(
                plan_id=plan.plan_id, decision_id=decision_id, pair=pair,
                direction=plan.direction, triggered_at=now, trigger_price=quote.mid,
                sl=action.get("sl"), tp=action.get("tp"), rr=action.get("rr"),
                snapshot_id=snapshot_id, risk_gate_result=risk_result,
            )
            # Plan C: trigger 確定と同時に pending hindsight 行を enqueue する。
            # horizon 経過後に hindsight loop が MFE-R/MAE-R/PnL-R を埋める。
            # evaluator 未注入 (Phase 1〜3) なら enqueue しない (shadow 境界・後方互換)。
            if self._hindsight is not None:
                self._orch.record_hindsight_evaluation(
                    shadow_trigger_id=shadow_trigger_id,
                    horizon_seconds=self._config.hindsight.horizon_seconds,
                )
            ok = True
            logger.info(
                f"[ORCH] 🧪 shadow trigger plan {plan.plan_id} {pair} {plan.direction} "
                f"@ {quote.mid}"
            )
            return True
        finally:
            self._orch.finish_run(run_id, status="ok" if ok else "failed")
            if not ok:
                # claim 後・記録前に落ちた: plan を active に戻し、次 tick で再評価させる
                # (triggered のまま放置すると shadow_trigger 行が無いのに再 trigger 不可になる)。
                self._orch.update_plan_status(plan.plan_id, "active")

    def _shadow_risk_precheck(self, plan, trigger_ctx: dict) -> dict | None:
        """ExecutionPlanDraft 相当を組んで RiskGateWorker.pre_check を回し dict 化する。

        plan の保存済み entry/action/invalidation から draft を復元する。復元できない
        (vocabulary 不整合 / expires_at 欠落等) 場合は None を返し trigger 記録は継続する
        (shadow なので risk reject でも発注はしない — 記録の欠落だけ許容)。
        """
        # expires_at は ExecutionPlanDraft で datetime 必須。nullable column なので
        # None のとき draft を作らず pre-check を skip する (Codex review #4)。
        if plan.expires_at is None:
            return None
        try:
            draft = ExecutionPlanDraft(
                direction=plan.direction,
                entry_conditions=[
                    EntryCondition.from_dict(c) for c in (plan.entry_conditions_json or [])
                ],
                action=dict(plan.action_json or {}),
                invalidation=[
                    InvalidationCondition.from_dict(c)
                    for c in (plan.invalidation_json or [])
                ],
                expires_at=plan.expires_at,
                reasoning_summary="shadow precheck",
            )
        except (SchemaParseError, ValueError):
            logger.warning(
                f"[ORCH] shadow risk pre-check skipped for plan {plan.plan_id}: "
                "could not reconstruct draft"
            )
            return None
        return self._risk_gate.pre_check(draft, trigger_ctx).to_dict()

    # ── ループ駆動 (enabled 時のみ) ─────────────────────────────

    def start(self) -> None:
        """enabled なら 2 ループスレッドを起動する。disabled なら何もしない。

        既に起動済み (スレッド生存中) の再 start() は no-op。これが無いと二重 start で
        スレッドが 4 本になり、最初の 2 本が参照を失って stop() で join 不能になる。
        """
        if not self._config.enabled:
            logger.info("[ORCH] orchestrator disabled — loops not started")
            return
        if self._planning_thread is not None and self._planning_thread.is_alive():
            logger.warning("[ORCH] start() called while already running — ignored")
            return
        self._stop.clear()
        self._planning_thread = threading.Thread(
            target=self._planning_loop, name="orch-planning", daemon=True
        )
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="orch-watch", daemon=True
        )
        self._planning_thread.start()
        self._watch_thread.start()
        # hindsight poll loop は evaluator 注入時のみ起動 (Phase 4)。
        if self._hindsight is not None:
            self._hindsight_thread = threading.Thread(
                target=self._hindsight_loop, name="orch-hindsight", daemon=True
            )
            self._hindsight_thread.start()
        logger.info(f"[ORCH] started (mode={self._config.mode}, pairs={self._pairs})")

    def stop(self) -> None:
        self._stop.set()
        for t in (self._planning_thread, self._watch_thread, self._hindsight_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._planning_thread = None
        self._watch_thread = None
        self._hindsight_thread = None

    def _planning_loop(self) -> None:
        wait = self._config.market_state.normal_seconds
        while not self._stop.is_set():
            try:
                self.run_planning_cycle()
            except Exception:
                logger.exception("[ORCH] planning loop iteration failed")
            self._stop.wait(timeout=wait)

    def _watch_loop(self) -> None:
        # Phase 1: tick stream 未接続のため固定の短間隔でポーリングする。
        # later plan で websocket tick 駆動に置き換える (§5.5)。
        while not self._stop.is_set():
            try:
                self.run_watch_cycle()
            except Exception:
                logger.exception("[ORCH] watch loop iteration failed")
            self._stop.wait(timeout=1.0)

    def _hindsight_loop(self) -> None:
        # horizon (既定 24h) 評価なので低頻度ポーリングで十分 (config.hindsight)。
        wait = self._config.hindsight.poll_interval_seconds
        while not self._stop.is_set():
            try:
                self.run_hindsight_cycle()
            except Exception:
                logger.exception("[ORCH] hindsight loop iteration failed")
            self._stop.wait(timeout=wait)
