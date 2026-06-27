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
import queue
import threading
from datetime import date, datetime, time as dtime
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
    from src.orchestrator.cadence_sources import MarketStateBridge
    from src.orchestrator.market_state_detector import MarketStateDetector
    from src.orchestrator.material_landing import MaterialLandingDetector
    from src.orchestrator.planning_pipeline import PipelineResult, PlanningPipeline
    from src.orchestrator.shadow_notifier import ShadowNotifier

logger = logging.getLogger(__name__)

QuoteProvider = Callable[[str], QuoteSnapshot]


def _side_of(direction: str | None) -> str | None:
    """plan direction (long/short) を decision.decision の side (buy/sell) に写像する。"""
    if direction == "long":
        return "buy"
    if direction == "short":
        return "sell"
    return None


def _resolve_pip_value(app_config, pair: str) -> float:
    """pair の pip_value を AppConfig.tradeable_instruments から引く (Task F)。

    既存 trading cycle (src/cycles/trading.py:301-308) と同じ instrument 設定経路を使う。
    見つからなければ 0.0 を返す (_calculate_position_size 側で fallback ロットになる)。
    """
    inst = next(
        (p for p in app_config.tradeable_instruments if p.symbol == pair), None
    )
    return inst.pip_value if inst is not None else 0.0


def _trade_signal_from_plan(plan, pair: str, quote, app_config, balance: float):
    """plan の action_json + quote から発注用 TradeSignal を構築する (Task F, codex High)。

    broker が読むフィールド (action/pair/position_size/stop_loss/take_profit/
    signal_reason/confidence) を plan から埋め、provenance フィールド (news/price/
    predicted_direction 等) は orchestrator 由来と分かる最小有効値で埋める。
    TradeSignal は 15 フィールド必須 (signal_combiner.py:21) なので全て渡す。

    position_size (codex High): ExecutionPlanDraft.action には position_size が無い
    (schemas.py の action は sl/tp/size_policy/rr/comment のみ) ため、既存 trading cycle と
    同じ risk ベース算出 (_calculate_position_size) を再利用する。固定 1000.0 fallback は
    risk_per_trade/min_lot_size/lot_unit を無視するため使わない。balance は execute 時点の
    口座残高で _execute_live_trigger が渡す (plan→発注の間に残高が動いても最新で sizing)。
    """
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from src.signals.signal_combiner import TradeSignal, _calculate_position_size

    action = plan.action_json or {}
    side = "buy" if plan.direction == "long" else "sell"
    bias = "bullish" if plan.direction == "long" else "bearish"
    pbias = "long" if plan.direction == "long" else "short"
    sl = float(action["sl"])
    tp = float(action["tp"])
    conf = float(action.get("confidence", 1.0))
    pip_value = _resolve_pip_value(app_config, pair)
    size = _calculate_position_size(
        balance=balance,
        risk_pct=app_config.trading.risk_per_trade,
        entry=quote.mid,
        stop_loss=sl,
        pip_value=pip_value,
        min_lot_size=app_config.trading.min_lot_size,
        lot_unit=app_config.trading.lot_unit,
    )
    now = db_now()

    news = NewsSentiment(pair=pair, sentiment_score=0.0, confidence=conf)
    price = PriceAnalysis(
        pair=pair, direction_bias=pbias, bias_score=0.0, confidence=conf,
        entry_zone=(quote.mid, quote.mid), reasoning_summary="orchestrator plan",
        analyzed_at=now, stop_loss=sl, take_profit=tp,
        risk_reward_ratio=float(action.get("rr", 0.0)),
    )
    return TradeSignal(
        pair=pair, action=side, predicted_direction=bias,
        combined_score=0.0, confidence=conf, entry_price=quote.mid,
        stop_loss=sl, take_profit=tp, position_size=size,
        signal_reason=f"orchestrator plan {plan.plan_id}",
        detail_reason="orchestrator watch trigger (Task F live execute)",
        news=news, price=price, generated_at=now,
    )


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
        quote_producer=None,
        protection_worker=None,
        detector: "MaterialLandingDetector | None" = None,
        pipeline: "PlanningPipeline | None" = None,
        evaluator: WatchEvaluator | None = None,
        risk_gate: RiskGateWorker | None = None,
        hindsight_evaluator: HindsightEvaluator | None = None,
        shadow_notifier: "ShadowNotifier | None" = None,
        market_state_detector: "MarketStateDetector | None" = None,
        state_bridge: "MarketStateBridge | None" = None,
        mode: str = "shadow",
        execution_broker=None,
        execution_position_mgr=None,
        app_config=None,
    ) -> None:
        self._config = config
        self._orch = orch_store
        self._ctx = context_builder
        self._pairs = pairs
        self._quote_provider = quote_provider
        # Phase 2/D: quote-stream producer (stage>=producer 時のみ注入)。注入時は
        # ループ起動前に start し、停止時に stop する。None なら従来 fetch 経路で無影響。
        self._quote_producer = quote_producer
        # protect_shadow 以上で注入される tick 駆動ポジション保護 worker (§5.2)。producer.latest を
        # 消費するため producer の後に start し、producer より先に stop する。None なら no-op。
        self._protection_worker = protection_worker
        self._detector = detector
        self._pipeline = pipeline
        # shadow 専用通知 (Phase 5)。未注入なら通知しない (後方互換・shadow 境界不変)。
        # 通知は deterministic path の外側で fire-and-forget し、失敗は握り潰す。
        self._notifier = shadow_notifier
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
        # 通知 worker (Codex Medium#1): 通知は HTTP 待ち (最大 10s) を伴うため、
        # planning/watch/hindsight ループ上で同期実行するとループが滞留する。専用 queue +
        # worker thread に逃がし、ループは enqueue だけで即戻る (真の fire-and-forget)。
        # worker 未起動時 (テスト・手動駆動) は同期実行にフォールバックする。
        self._notify_queue: "queue.Queue[Callable[[], object] | None]" = queue.Queue()
        self._notify_thread: threading.Thread | None = None
        # daily summary (Phase1 A-1): 1 日 1 回ガード。最後に送った日付を持ち、設定時刻を
        # 跨いだ最初の cycle で 1 回だけ送る。notifier 未注入 / flag off なら loop を起こさない。
        self._daily_summary_thread: threading.Thread | None = None
        self._last_summary_date: date | None = None
        # check-then-set (今日送信済みか) を直列化する。loop スレッドと手動呼び出しが
        # 同時に guard を通過して二重送信するのを防ぐ (code review High#2)。
        self._summary_lock = threading.Lock()
        # market state 検知 (Phase1 Task C)。両方注入されたときのみ state ループを起動し、
        # cadence boost (経路②) + regime 変化トリガを駆動する。未注入なら起動しない。
        self._mstate = market_state_detector
        self._state_bridge = state_bridge
        self._mstate_thread: threading.Thread | None = None
        # pair -> 直近 mid (move_pct 算出用)。
        self._last_mid: dict[str, float] = {}
        # Task F: 本番発注 (mode=live) の執行段。shadow では execution_broker=None で
        # live 分岐に入らない (回帰維持)。app_config は position_size の risk 算出
        # (risk_per_trade / pip_value) に使う (OrchestratorConfig には無いため)。
        self._mode = mode
        self._execution_broker = execution_broker
        self._execution_position_mgr = execution_position_mgr
        self._app_config = app_config

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
                        # plan_create / reject を shadow 通知 (direct_hold は通知しない)。
                        # 記録完了後・deterministic path の外で fire する。
                        self._notify_planning_result(pair, result)
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
            except Exception as exc:
                # provider 例外 / OHLCV カラム欠損 / tz 差分など deterministic な評価不能を
                # pending に残すと同じ行が毎 poll 再実行され続ける (Codex Medium)。failed に
                # 倒し再 query されないようにする。failed 化自体が落ちても他行は止めない。
                logger.exception(
                    f"[ORCH] hindsight eval failed for trigger {ev.shadow_trigger_id}"
                )
                try:
                    self._orch.update_hindsight_evaluation(
                        ev.id, status="failed", evaluated_at=now,
                        reasoning_summary=f"eval error: {type(exc).__name__}: {exc}",
                    )
                    evaluated += 1
                except Exception:
                    logger.exception(
                        f"[ORCH] could not mark hindsight {ev.id} failed"
                    )
        return evaluated

    def run_daily_summary_cycle(self, now: datetime | None = None) -> bool:
        """1 日 1 回 shadow daily summary を送る (§11 / Phase1 A-1)。

        `daily_summary_time` を跨いだ最初の cycle で 1 回だけ送り、その日付を記録する。
        同日二度目は no-op。notifier 未注入 / `shadow_daily_summary` off なら何もしない。

        Returns: 送信した (= cycle で発火した) なら True。
        """
        if self._notifier is None or not self._config.notifications.shadow_daily_summary:
            return False
        now = now or db_now()
        today = now.date()
        if now.time() < self._parse_summary_time():
            return False  # まだ送信時刻前 (lock 不要の早期 return)
        from src.orchestrator.shadow_metrics import compute_shadow_metrics

        # lock 内で check + metrics 計算 + 日付確定をまとめて原子化する:
        # - 二重送信防止 (concurrent caller を直列化, High#2)。
        # - metrics 計算が成功してから日付を確定する (失敗時は未送信扱いのまま、当日中の
        #   後続 cycle で再試行できる, code review Medium#4)。metrics は軽量 SQL 集計なので
        #   lock 内実行で問題ない。
        with self._summary_lock:
            if self._last_summary_date == today:
                return False  # 同日は送信済み
            try:
                metrics = compute_shadow_metrics(self._orch, now=now)
            except Exception:
                logger.exception("[ORCH] daily summary metrics 計算に失敗 — 当日再試行する")
                return False  # 日付を確定しない (次 cycle で再試行)
            self._last_summary_date = today
        # enqueue は lock の外で (通知 worker への引き渡しのみ。HTTP 待ちは worker 側)。
        n = self._notifier
        self._run_notify(lambda: n.notify_daily_summary(metrics, day=now))
        logger.info(f"[ORCH] 🧪 shadow daily summary fired ({today})")
        return True

    def run_market_state_cycle(self, now: datetime | None = None) -> dict[str, str]:
        """全 pair の市場 state を 1 周更新する (Phase1 Task C)。

        **Phase1 のスコープは「価格変化率ベース」に限定する (code review Medium#3)。**
        quote provider から mid (と spread が取れれば spread) を取り、前回 mid との差で
        move_pct を出して detector に渡す。detector は本来 bridge_degraded / SL-TP 近接 /
        important_news も critical/active 判定に使えるが、Phase1 ではそれらの provider を
        接続せず move_pct (+ spread が利用可能なら spread) のみで判定する:

        - spread: 現行 quote provider は spread=None を返す (bid/ask 非取得)。実 spread は
          **Phase2/D の websocket tick 基盤**で供給される。それまで spread 経路は不活性。
        - position 近接 / bridge: PriceMonitorWorker (§5.5) と risk_state 側の責務で、
          market state loop は軽量監視に留める。これらの接続も Phase2/D 以降。

        実質「価格変化率 (+ 将来 spread)」での state 判定。detector で state を判定し、
        state_bridge 経由で cadence boost (経路②) と regime 変化トリガを駆動する。
        detector/bridge 未注入なら no-op。

        **執行は制御しない (§5.2)。** boost と planning トリガにのみ効く。

        Returns: {pair: state} (観測用)。
        """
        if self._mstate is None:
            return {}
        from src.orchestrator.market_state_detector import MarketObservation

        now = now or db_now()
        out: dict[str, str] = {}
        for pair in self._pairs:
            try:
                quote = self._quote_provider(pair)
            except Exception:
                logger.exception(f"[ORCH] market state quote 取得失敗 ({pair})")
                continue
            move_pct = self._move_pct(pair, quote.mid)
            spread_pips = self._spread_pips(pair, quote.spread)
            obs = MarketObservation(
                move_pct=move_pct, spread_pips=spread_pips,
                # position / bridge / news は Phase1 では未接続 (上 docstring 参照)。
                # 実質 move_pct (+ 将来 spread) ベースの判定。
            )
            state = self._mstate.observe(pair, obs, now)
            out[pair] = state
            if self._state_bridge is not None:
                self._state_bridge.update(pair, state, now)
        return out

    def _move_pct(self, pair: str, mid: float | None) -> float:
        """前回 mid からの変動率 (絶対値, **パーセント値**)。初回 / mid 欠落は 0。

        単位は active_move_pct と揃える (% 値、例: 0.15 = 0.15%、code review High#1)。
        """
        if mid is None:
            return 0.0
        prev = self._last_mid.get(pair)
        self._last_mid[pair] = mid
        if prev is None or prev == 0:
            return 0.0
        return abs(mid - prev) / prev * 100.0

    @staticmethod
    def _spread_pips(pair: str, spread: float | None) -> float | None:
        """spread (価格差) を pips に変換。None は不明として透過。"""
        if spread is None:
            return None
        pip_size = 0.01 if pair.upper().endswith("JPY=X") or "JPY" in pair.upper() else 0.0001
        return spread / pip_size

    def _parse_summary_time(self) -> dtime:
        """`daily_summary_time` (HH:MM) を datetime.time に parse。不正値は 07:00 に倒す。"""
        raw = self._config.notifications.daily_summary_time or "07:00"
        try:
            h, m = raw.split(":")
            return dtime(int(h), int(m))
        except (ValueError, TypeError):
            logger.warning(f"[ORCH] 不正な daily_summary_time={raw!r} — 07:00 を使用")
            return dtime(7, 0)

    def _evaluate_one_hindsight(self, ev, now: datetime) -> bool:
        """1 pending hindsight 行を評価し DB に書き戻す。評価を試みたら True。"""
        trig = self._orch.get_shadow_trigger_by_id(ev.shadow_trigger_id)
        if trig is None:
            # 孤児 pending を pending のままにすると horizon 超過で毎 poll 再評価され続ける。
            # failed に倒して二度と get_pending に拾われないようにする。
            logger.warning(
                f"[ORCH] hindsight: shadow_trigger {ev.shadow_trigger_id} missing — "
                "marking evaluation failed"
            )
            self._orch.update_hindsight_evaluation(
                ev.id, status="failed", evaluated_at=now,
                reasoning_summary="shadow_trigger row missing",
            )
            return True
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
        # 評価成功のみ shadow 通知 (failed は通知しない — ノイズ抑制)。
        self._notify_hindsight(trig, result)
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
        action: dict = plan.action_json or {}  # finally 後の通知でも参照するため try 外で確定
        try:
            # 新規 snapshot を materialize (planning 時点とは別。trigger 時の quote/freshness/
            # risk pre-check を独立 trace する — §7.1)。
            trigger_ctx = self._ctx.build(pair=pair, now=now, quote=quote)
            snapshot_id = trigger_ctx["snapshot_id"]
            self._orch.attach_snapshot(run_id, snapshot_id)

            # shadow risk pre-check。Phase 2 は hard veto にせず結果を残すだけ。
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
            # Task F: live mode かつ execution broker 注入時のみ本番発注へ進む (single
            # writer)。try 内・run が open のうちに呼ぶ (record_decision を open run に
            # 正しく紐づけ、finish_run 後の後付けを避ける, codex High)。例外は
            # _execute_live_trigger 内で握り、shadow trigger 記録 (ok=True) は倒さない。
            if self._mode == "live" and self._execution_broker is not None:
                self._execute_live_trigger(
                    plan, pair, quote, decision_id, snapshot_id, run_id,
                    trigger_id=str(shadow_trigger_id), trigger_ctx=trigger_ctx,
                )
        finally:
            self._orch.finish_run(run_id, status="ok" if ok else "failed")
            if not ok:
                # claim 後・記録前に落ちた: plan を active に戻し、次 tick で再評価させる
                # (triggered のまま放置すると shadow_trigger 行が無いのに再 trigger 不可になる)。
                self._orch.update_plan_status(plan.plan_id, "active")
        # 通知は run lifecycle (finish_run) を閉じた後に fire する: 同期 HTTP POST の遅延を
        # run の確定より後ろに置き、watch loop の hot path 内滞留を避ける (review MEDIUM)。
        # ok のときだけ通知 (失敗 trigger は通知しない)。
        if not ok:
            return False
        self._notify_shadow_trigger(plan, pair, quote, action)
        return True

    # ── Task F: 本番発注 執行段 (F-1 / F-3) ─────────────────────

    _EXECUTION_LEASE_SECONDS = 120

    def _execute_live_trigger(
        self, plan, pair: str, quote, decision_id, snapshot_id, run_id,
        *, trigger_id=None, trigger_ctx=None,
    ) -> None:
        """本番発注の執行段 (spec §2, F-1/F-3)。single execution writer。

        claim (order_intent) → final gate → submit-marked → execute → 結果反映。
        run はまだ open (呼び出しは finish_run より前)。record_decision には渡された
        snapshot_id を使う。final gate は trigger 時の trigger_ctx を再利用し、保存 snapshot
        と監査整合させる (codex Medium)。trigger_id (= shadow_trigger_id) を order_intent に
        記録し trace を繋ぐ。例外はフェーズ別に即時記録 (codex High、下記 except)。
        """
        from datetime import timedelta

        from src.trading.order_intent_status import (
            intent_status_for_outcome,
            is_alertable_outcome,
        )

        # 例外フェーズ追跡 (codex High): broad catch が intent を放置すると、daemon 生存中は
        # recovery (bootstrap 1 回のみ) に拾われず plan が triggered のまま永久ブロックする。
        # claim 済かつ submit 前で落ちたら abandoned+invalidated (未発注 = 新 plan で replan)、
        # submit 後で落ちたら needs_reconcile (建玉不明 = 隔離) を except で即時記録する。
        claimed = False
        submitted = False
        try:
            # 1. claim (UNIQUE で二重発注防止)
            lease = db_now() + timedelta(seconds=self._EXECUTION_LEASE_SECONDS)
            claimed = self._orch.try_insert_order_intent(
                plan_id=plan.plan_id, pair=pair,
                intended_action=_side_of(plan.direction) or "buy",
                owner_run_id=run_id, lease_until=lease, decision_id=decision_id,
                trigger_id=trigger_id,
            )
            if not claimed:
                logger.info(
                    f"[ORCH] plan {plan.plan_id} already has order_intent — execute skipped"
                )
                return

            # 1.5 material recheck (spec §2 step2、既定 OFF)。ON 時のみ ExecutionOpinionAgent
            #     を再点火する分岐をここに置く (将来 task — 現状は配線のプレースのみ)。
            if self._config.execution_opinion_recheck_enabled:
                logger.debug(
                    "[ORCH] material recheck flag on — (future) ExecutionOpinion 再点火"
                )

            # 2. final gate (RiskGate pre_check を hard gate として使う)。draft 復元
            #    不能 (expires_at 欠落等) のときは hard reject せず構造 reject 扱いにする。
            draft = self._build_execution_draft(plan, reasoning="live final gate")
            if draft is None:
                self._orch.record_order_result(plan_id=plan.plan_id, status="rejected")
                self._apply_reject_transition(
                    plan, pair, "structural", run_id, snapshot_id, None,
                    reason="live gate: draft reconstruction failed",
                )
                logger.warning(
                    f"[ORCH] live gate reject plan {plan.plan_id}: draft unbuildable"
                )
                return
            # final gate は trigger 時の context を再利用する (codex Medium): 別途
            # assemble(now) で組み直すと gate 判断が保存 snapshot で再現できなくなる。
            # trigger_ctx 未提供時 (直接呼出テスト等) のみ assemble に fallback する。
            gate_ctx = trigger_ctx if trigger_ctx is not None else \
                self._ctx.assemble(pair=pair, now=db_now(), quote=quote)
            gate = self._risk_gate.pre_check(draft, gate_ctx)
            if not gate.passed:
                # 恒久 (structural)=rejected / 一時 (fixable)=abandoned で intent.status を区別。
                # どちらも plan は invalidated (terminal)。再発注は新 plan_id で行う (replan)。
                intent_status = "rejected" if gate.reject_class == "structural" else "abandoned"
                self._orch.record_order_result(plan_id=plan.plan_id, status=intent_status)
                self._apply_reject_transition(
                    plan, pair, gate.reject_class, run_id, snapshot_id, gate.to_dict(),
                    reason=f"live gate reject ({gate.reject_class})",
                )
                logger.warning(
                    f"[ORCH] live gate reject plan {plan.plan_id}: {gate.issues}"
                )
                return

            # 3. signal 構築 (broker 送信前 = 復旧分岐点の前)。sizing/signal 構築は broker
            #    side effect が無いので submit マーキングより前に置く。ここで例外
            #    (pip_value=0 ZeroDivision / sl-tp 欠落 KeyError 等) が出ても order_intent は
            #    pending のまま → recovery が retryable で扱える (false needs_reconcile を避ける、
            #    code review M1/M2)。balance は disk から reload して最新値で sizing する
            #    (execution position_mgr は長命で他経路の決済を取りこぼすため、M1)。
            self._execution_position_mgr.reload()
            balance = self._execution_position_mgr.get_account_state().balance
            signal = _trade_signal_from_plan(plan, pair, quote, self._app_config, balance)

            # 4. submit マーキング (復旧分岐点)。この直後〜broker 応答前にクラッシュすると
            #    status=submitted かつ order_id null になり、建玉不明として needs_reconcile で
            #    隔離する。よって submit マークは broker 送信の直前に置く。submitted フラグを
            #    立て、以降の例外は「送信後」として except で needs_reconcile に倒す。
            self._orch.mark_order_submitted(plan_id=plan.plan_id, submitted_at=db_now())
            submitted = True

            # 5. 発注 (single writer)
            result = self._execution_broker.execute_signal(
                signal, self._execution_position_mgr,
            )

            # 6. 結果反映 (outcome → status mapping)
            status = intent_status_for_outcome(result.outcome)
            order_id = result.order.order_id if result.is_executed and result.order else None
            self._orch.record_order_result(
                plan_id=plan.plan_id, status=status, order_id=order_id,
                broker_result_json={"outcome": result.outcome, "reason": result.reason},
            )
            self._orch.record_decision(
                run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                decision_type="plan_trigger", plan_id=plan.plan_id,
                reasoning_summary=f"live execute: {result.outcome}",
                order_id=order_id,
            )
            if not result.is_executed:
                # 発注に至らなかった (skipped/halted/rejected/failed): plan を terminal 化。
                # 再発注は次 planning が新 plan を作る (replan)。UNIQUE 行は残るが新 plan_id
                # なので衝突しない (codex High#2: status 変更では UNIQUE 解放されない)。
                self._orch.update_plan_status(plan.plan_id, "invalidated")
            elif result.outcome == "executed":
                # F-5: executed は is_alertable_outcome==False で warning が出ないため INFO を
                # 足す (体裁は 🧪 shadow trigger と揃える)。order_id は result.order から取る
                # (ExecutionResult に直属 order_id 属性は無い, codex Medium)。
                logger.info(
                    "[ORCH] ✅ live execute plan %s %s @ %s",
                    plan.plan_id, pair, order_id,
                )
            if is_alertable_outcome(result.outcome):
                logger.warning(
                    f"[ORCH] live execute {result.outcome} plan {plan.plan_id}: "
                    f"{result.reason}"
                )
        except Exception:
            logger.exception(f"[ORCH] live execute failed for plan {plan.plan_id}")
            # codex High: フェーズ別に即時記録し、daemon 生存中も plan が triggered のまま
            # 放置されない (recovery は bootstrap 1 回のみ)。記録自体が失敗しても watch loop は
            # 止めない (二重 except)。
            self._record_execution_failure(plan, run_id, snapshot_id, claimed, submitted)

    def _record_execution_failure(
        self, plan, run_id, snapshot_id, claimed: bool, submitted: bool,
    ) -> None:
        """_execute_live_trigger の例外をフェーズ別に terminal/needs_reconcile 記録する。

        - claim 前で落ちた (claimed=False): order_intent 行が無いので何もしない。plan は
          finally で active に戻され次 tick で再評価される (codex High は claim 後が対象)。
        - submit 前で落ちた (claimed=True, submitted=False): 未発注。intent=abandoned +
          plan=invalidated (新 plan で replan)。
        - submit 後で落ちた (submitted=True): broker 応答前にクラッシュ = 建玉不明。
          needs_reconcile で隔離 (intent/plan は触らず、手動/既存 reconciliation 待ち)。
        """
        try:
            if not claimed:
                return
            if submitted:
                self._orch.set_recovery_status(
                    plan_id=plan.plan_id, recovery_status="needs_reconcile",
                )
                logger.warning(
                    "[ORCH-RECOVERY] needs_reconcile (in-process): plan %s submitted but "
                    "broker result unknown — 再 trigger 禁止・要手動確認", plan.plan_id,
                )
            else:
                self._orch.record_order_result(plan_id=plan.plan_id, status="abandoned")
                self._orch.update_plan_status(plan.plan_id, "invalidated")
                logger.warning(
                    "[ORCH] live execute aborted before submit for plan %s — "
                    "intent=abandoned, plan=invalidated (replan)", plan.plan_id,
                )
        except Exception:
            logger.exception(
                f"[ORCH] failed to record execution failure state for plan {plan.plan_id}"
            )

    def _apply_reject_transition(
        self, plan, pair: str, reject_class, run_id, snapshot_id, risk_gate_result,
        *, reason: str,
    ) -> None:
        """gate reject 後の plan 遷移 (spec §4, codex #4)。plan を invalidated (terminal)
        にし plan_invalidate decision を残す。再発注は次 planning が新 plan を作る (replan)
        ため、triggered のまま残して永久ブロックになるのを防ぐ。intent.status は呼び出し側で
        既に terminal 化済 (rejected/abandoned)。
        """
        self._orch.update_plan_status(plan.plan_id, "invalidated")
        self._orch.record_decision(
            run_id=run_id, snapshot_id=snapshot_id, pair=pair,
            decision_type="plan_invalidate", plan_id=plan.plan_id,
            reasoning_summary=reason, risk_gate_result=risk_gate_result,
        )

    def _shadow_risk_precheck(self, plan, trigger_ctx: dict) -> dict | None:
        """ExecutionPlanDraft 相当を組んで RiskGateWorker.pre_check を回し dict 化する。

        plan の保存済み entry/action/invalidation から draft を復元する。復元できない
        (vocabulary 不整合 / expires_at 欠落等) 場合は None を返し trigger 記録は継続する
        (shadow なので risk reject でも発注はしない — 記録の欠落だけ許容)。
        """
        draft = self._build_execution_draft(plan, reasoning="shadow precheck")
        if draft is None:
            return None
        return self._risk_gate.pre_check(draft, trigger_ctx).to_dict()

    def _build_execution_draft(self, plan, *, reasoning: str):
        """plan の保存済み条件から ExecutionPlanDraft を復元する (shadow precheck と live
        final gate で共有, Task F DRY)。expires_at 欠落 / vocabulary 不整合なら None。
        """
        # expires_at は ExecutionPlanDraft で datetime 必須。nullable column なので
        # None のとき draft を作らず pre-check を skip する (Codex review #4)。
        if plan.expires_at is None:
            return None
        try:
            return ExecutionPlanDraft(
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
                reasoning_summary=reasoning,
            )
        except (SchemaParseError, ValueError):
            logger.warning(
                f"[ORCH] risk draft reconstruction failed for plan {plan.plan_id}"
            )
            return None

    # ── shadow 通知 (Phase 5, worker thread で非ブロッキング) ────

    def _start_notify_worker(self) -> None:
        """通知 worker thread を起動する。二重起動は no-op。

        notifier 未注入時は worker を起こさない (通知が無いので不要)。
        """
        if self._notifier is None:
            return
        if self._notify_thread is not None and self._notify_thread.is_alive():
            return
        self._notify_thread = threading.Thread(
            target=self._notify_worker_loop, name="orch-notify", daemon=True
        )
        self._notify_thread.start()

    def _stop_notify_worker(self) -> None:
        """sentinel を積んで worker に残りを drain させ、join する。"""
        t = self._notify_thread
        if t is None:
            return
        self._notify_queue.put(None)  # sentinel
        t.join(timeout=15.0)  # Discord timeout(10s) + マージン
        self._notify_thread = None

    def _notify_worker_loop(self) -> None:
        """queue から通知ジョブ (coroutine factory) を取り出し順次実行する。

        ジョブは「呼ぶと coroutine を返す callable」。enqueue 時点で coroutine 化すると
        未 await 警告が出るため factory にしている。None は stop sentinel。
        """
        while True:
            factory = self._notify_queue.get()
            if factory is None:
                return
            self._execute_notify(factory)

    def _run_notify(self, factory: "Callable[[], object]") -> None:
        """通知ジョブを worker に enqueue する。worker 未起動なら同期実行。

        worker 起動中はループをブロックしない (enqueue は即時)。未起動時 (テスト・手動
        駆動) は後方互換のためその場で実行する。
        """
        if self._notify_thread is not None and self._notify_thread.is_alive():
            self._notify_queue.put(factory)
        else:
            self._execute_notify(factory)

    @staticmethod
    def _execute_notify(factory: "Callable[[], object]") -> None:
        """factory() で coroutine を生成し asyncio.run で実行。失敗は握り潰す。

        通知は deterministic path の付随物であり、送信失敗・notifier 不整合が記録系を
        止めてはならない (§通知失敗はシステムを止めない)。同期スレッドから呼ぶ前提
        (asyncio.run はネストした running loop からは呼べない)。
        """
        try:
            asyncio.run(factory())
        except Exception:
            logger.warning("[ORCH] shadow notification failed", exc_info=True)

    def _notify_planning_result(self, pair: str, result: "PipelineResult") -> None:
        """plan_create / reject を shadow 通知する。direct_hold/failed は通知しない。"""
        # F-5 (spec §F-5): plan 作成成功をターミナルログにも出す (shadow trigger 🧪 と対)。
        # plan_create のときのみ。direct_hold / failed は出さない (既存通知方針と同じ)。
        # notifier 未注入でも出す (mode 非依存の観測性改善) ため guard の前に置く。
        if result.outcome == "plan_create" and result.plan_id is not None:
            logger.info(
                "[ORCH] 📋 plan created %s %s %s score=%+.2f conf=%.2f",
                result.plan_id, pair, result.direction or "?",
                result.score if result.score is not None else 0.0,
                result.confidence if result.confidence is not None else 0.0,
            )

        if self._notifier is None:
            return
        from src.orchestrator.shadow_notifier import PlanCreatedInfo

        n = self._notifier
        if result.outcome == "plan_create" and result.plan_id is not None:
            new_id = result.plan_id
            for old_id in result.superseded_plan_ids:
                self._run_notify(
                    lambda old_id=old_id: n.notify_plan_superseded(
                        pair=pair, old_plan_id=old_id, new_plan_id=new_id,
                    )
                )
            info = PlanCreatedInfo(
                pair=pair, direction=result.direction or "?",
                plan_id=new_id, score=result.score,
                confidence=result.confidence, reason=result.reason or "",
            )
            self._run_notify(lambda: n.notify_plan_created(info))
        elif result.outcome == "reject":
            reason = result.reason or "rejected"
            self._run_notify(
                lambda: n.notify_plan_rejected(pair=pair, reason=reason)
            )

    def _notify_shadow_trigger(self, plan, pair: str, quote: QuoteSnapshot, action: dict) -> None:
        if self._notifier is None:
            return
        from src.orchestrator.shadow_notifier import ShadowTriggerInfo

        n = self._notifier
        info = ShadowTriggerInfo(
            pair=pair, direction=plan.direction or "?", plan_id=plan.plan_id,
            score=None, confidence=None,  # plan 行に score/conf は無い (§trigger)
            trigger_price=quote.mid,
            sl=action.get("sl"), tp=action.get("tp"), rr=action.get("rr"),
            reason=(action.get("comment") or ""),
        )
        self._run_notify(lambda: n.notify_shadow_trigger(info))

    def _notify_hindsight(self, trig, result) -> None:
        if self._notifier is None:
            return
        from src.orchestrator.shadow_notifier import HindsightInfo

        n = self._notifier
        info = HindsightInfo(
            pair=trig.pair, direction=trig.direction, plan_id=trig.plan_id,
            mfe_r=result.mfe_r, mae_r=result.mae_r, pnl_r=result.pnl_r,
            would_hit_tp=result.would_hit_tp, would_hit_sl=result.would_hit_sl,
        )
        self._run_notify(lambda: n.notify_hindsight_evaluated(info))

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
        # quote-stream producer を最初に起こす: watch/planning ループが latest を読む前に
        # poll を開始しておく (consumer より先に起動)。未注入なら no-op。
        if self._quote_producer is not None:
            self._quote_producer.start()
        # 保護 worker は producer の後に起こす: producer.latest を読むため、consumer として
        # producer 起動後に立てる (§5.2)。未注入なら no-op。
        if self._protection_worker is not None:
            self._protection_worker.start()
        # 通知 worker を先に起こす: ループが enqueue する前に worker を立てておく。
        self._start_notify_worker()
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
        # daily summary loop は notifier 注入 + flag on のときだけ起動 (Phase1 A-1)。
        if self._notifier is not None and self._config.notifications.shadow_daily_summary:
            self._daily_summary_thread = threading.Thread(
                target=self._daily_summary_loop, name="orch-daily-summary", daemon=True
            )
            self._daily_summary_thread.start()
        # market state loop は detector 注入時のみ起動 (Phase1 Task C)。
        if self._mstate is not None:
            self._mstate_thread = threading.Thread(
                target=self._market_state_loop, name="orch-market-state", daemon=True
            )
            self._mstate_thread.start()
        logger.info(f"[ORCH] started (mode={self._config.mode}, pairs={self._pairs})")

    def stop(self) -> None:
        self._stop.set()
        for t in (
            self._planning_thread, self._watch_thread,
            self._hindsight_thread, self._daily_summary_thread,
            self._mstate_thread,
        ):
            if t is not None:
                t.join(timeout=2.0)
        self._planning_thread = None
        self._watch_thread = None
        self._hindsight_thread = None
        self._daily_summary_thread = None
        self._mstate_thread = None
        # ループ停止後に通知 worker を drain して止める (in-flight 通知を取りこぼさない)。
        self._stop_notify_worker()
        # 保護 worker は producer より先に止める: producer.latest を読む consumer なので、
        # producer を残したまま consumer を畳む (§5.2)。未注入なら no-op。
        if self._protection_worker is not None:
            self._protection_worker.stop()
        # consumer (watch/planning/protection) 停止後に producer を止める (最後に停止)。未注入なら no-op。
        if self._quote_producer is not None:
            self._quote_producer.stop()

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

    def _daily_summary_loop(self) -> None:
        # 日次判定なので粗いポーリングで十分。market_state の timing とは独立させる
        # (code review Medium#2 — 結合回避)。固定 300s。run_daily_summary_cycle 内の
        # 1 日 1 回ガードが多重送信を防ぐ。
        wait = 300
        while not self._stop.is_set():
            try:
                self.run_daily_summary_cycle()
            except Exception:
                logger.exception("[ORCH] daily summary loop iteration failed")
            self._stop.wait(timeout=wait)

    def _market_state_loop(self) -> None:
        # state 検知は active_seconds 程度の周期で軽量にポーリングする (§5.2)。急変即応では
        # ない (執行は watch loop)。boost/regime トリガのための観測更新が目的。
        wait = self._config.market_state.active_seconds
        while not self._stop.is_set():
            try:
                self.run_market_state_cycle()
            except Exception:
                logger.exception("[ORCH] market state loop iteration failed")
            self._stop.wait(timeout=wait)
