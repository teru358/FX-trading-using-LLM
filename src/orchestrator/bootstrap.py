"""OrchestratorRuntime の本番 lifecycle 結線 (Phase 6 Task 6.1)。

main.py から呼び、`config.orchestrator.enabled` が true のときだけ全部品を組み立てて
`OrchestratorRuntime` を返す。disabled なら None を返し、既存 trading cycle に一切影響
させない。

shadow 境界 (§7.3): broker / order_intents には触れない。本 bootstrap は broker adapter
を runtime に渡さない (発注 path を構築しない)。pairs は **tradeable instruments のみ**
(watch 除外、§4.8 / plan Task 6.1)。

部品の実結線 (Phase 2〜5 で factory/注入点だけ用意し production 未結線だったもの):
  - make_news_provider(config, store)        — §7 news を RAG 集計から供給
  - make_hindsight_evaluator(price_store)    — trigger 後 MFE-R/MAE-R/PnL-R 評価
  - create_shadow_notifier(notifications cfg) — 🧪 shadow 専用 Discord 通知
  - LlmDispatcher + PlanningPipeline         — planning loop の LLM 逐次パイプライン
  - MaterialLandingDetector                  — planning 発火フィルタ (material + debounce)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.orchestrator.context_builder import (
    DecisionContextBuilder,
    QuoteSnapshot,
    make_news_provider,
)
from src.orchestrator.hindsight_evaluator import make_hindsight_evaluator
from src.orchestrator.material_landing import MaterialLandingDetector
from src.orchestrator.runtime import OrchestratorRuntime, QuoteProvider
from src.orchestrator.shadow_notifier import create_shadow_notifier
from src.trading.market_state import market_skip_check
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.config.schema import AppConfig
    from src.data.analysis_store import AnalysisStore
    from src.data.orchestrator_store import OrchestratorStore
    from src.data.price_provider import PriceProvider
    from src.data.price_store import PriceStore
    from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


def make_quote_provider(price_provider: "PriceProvider") -> QuoteProvider:
    """PriceProvider.get_current_price を §7 QuoteSnapshot provider に適合させる。

    CurrentPrice は bid/ask を持たず price のみ (yfinance/TD/MT5 共通形)。実 spread が
    取れないため **spread=None (不明)** とする (Codex Low-Medium)。spread=0 にすると spread
    gate を楽観的に必ず通し shadow 検証値が歪むため、不明として gate に安全側 (block/reject)
    で扱わせる。実 spread (bid/ask) の tick 受信は websocket 化の後続フェーズ。
    """

    def provider(pair: str) -> QuoteSnapshot:
        cp = price_provider.get_current_price(pair)
        # observed_at は DB 規約 (db_now = naive ローカル) に揃える。CurrentPrice.timestamp
        # が無ければ now で補完する。context_builder は isoformat して snapshot に保存する。
        observed = cp.timestamp or db_now()
        return QuoteSnapshot(
            bid=cp.price, ask=cp.price, mid=cp.price, spread=None,
            source=cp.source, observed_at=observed,
        )

    return provider


def _read_halt(config: "AppConfig") -> tuple[bool, str, str]:
    """halt 状態を (halted, halt_level, bridge_health) に正規化する。

    bridge の live probe は副作用 (再 halt 発動) と遅延があるため毎 cycle 叩かない。
    bridge 不通は既存設計で halt に反映される (BridgeHealthGate が soft halt を発動) ため、
    halt 状態から bridge_health を導出する: auto_triggered な halt = bridge/health 由来の
    可能性が高い → degraded、手動 halt は ok 扱い。
    """
    from src.persistence import halt_state

    state = halt_state.read(config.state_dir)
    if not state.soft_halted:
        return (False, "none", "ok")
    bridge = "degraded" if state.auto_triggered else "ok"
    return (True, "soft", bridge)


def make_risk_state_provider(config: "AppConfig"):
    """market_skip_check + halt_state を §7 risk_state に集約する provider。

    休場中 (market_skip_check) は market_open=False、soft halt 中は halt='soft' を返す。
    これを DecisionContextBuilder に注入することで、休場/halt 中の planning は risk_gate
    structural reject、watch trigger は freshness_issues の market/halt wall で止まる
    (Codex High: 固定 risk_state による gate 素通りの是正)。
    """

    def provider(pair: str) -> dict:
        halted, halt_level, bridge_health = _read_halt(config)
        return {
            "halt": halt_level,
            "bridge_health": bridge_health,
            "market_open": not market_skip_check(),
            # cooldown (timeout 後の再エントリ抑制) は pair 別状態で別管理。ここでは
            # 集約せず False (re-entry guard は RiskGateWorker 側 §4.5 の後続作業)。
            "cooldown": False,
        }

    return provider


def build_orchestrator_runtime(
    config: "AppConfig",
    *,
    store: "VectorStore",
    price_store: "PriceStore",
    analysis_store: "AnalysisStore",
    price_provider: "PriceProvider",
) -> OrchestratorRuntime | None:
    """enabled なら OrchestratorRuntime を組み立てて返す。disabled なら None。

    既存の trading 系 store/provider を再利用し、orchestrator 専用 trace は
    OrchestratorStore (prices_db_path 上の追加テーブル) に残す。
    """
    orch_cfg = config.orchestrator
    if not orch_cfg.enabled:
        logger.info("[ORCH] orchestrator disabled — runtime not built")
        return None

    # 遅延 import: OrchestratorStore は SQLAlchemy ORM を引き込むため、disabled 時に
    # コストを払わない (main.py の他経路と同方針)。
    from src.data.orchestrator_store import OrchestratorStore

    orch_store = OrchestratorStore(config.prices_db_path)

    # pairs は tradeable のみ (watch は planning/trigger 対象外、§4.8)。orch.pairs が
    # 指定されていれば tradeable との intersection に絞る (orchestrator だけ USDJPY に
    # 限定する等の運用、Codex Medium)。watch/未知 symbol は warning で除外する。
    tradeable = [inst.symbol for inst in config.tradeable_instruments]
    configured = list(orch_cfg.pairs or [])
    if configured:
        tradeable_set = set(tradeable)
        pairs = [s for s in configured if s in tradeable_set]
        dropped = [s for s in configured if s not in tradeable_set]
        if dropped:
            logger.warning(
                "[ORCH] orchestrator.pairs に tradeable でない symbol が含まれ除外: %s",
                dropped,
            )
    else:
        pairs = tradeable
    if not pairs:
        logger.warning(
            "[ORCH] no tradeable pairs to orchestrate (configured=%s) — runtime not built",
            configured or "<all tradeable>",
        )
        return None

    context_builder = DecisionContextBuilder(
        orch_store, analysis_store, orch_cfg,
        news_provider=make_news_provider(config, store),
        risk_state_provider=make_risk_state_provider(config),
    )

    quote_provider = make_quote_provider(price_provider)

    detector = _build_detector(config, orch_cfg, analysis_store, pairs, store=store)
    pipeline = _build_pipeline(config, orch_store)
    hindsight = make_hindsight_evaluator(price_store)
    notifier = create_shadow_notifier(orch_cfg.notifications)

    mstate, state_bridge = _build_market_state(config, orch_cfg, pairs)

    runtime = OrchestratorRuntime(
        config=orch_cfg,
        orch_store=orch_store,
        context_builder=context_builder,
        pairs=pairs,
        quote_provider=quote_provider,
        detector=detector,
        pipeline=pipeline,
        hindsight_evaluator=hindsight,
        shadow_notifier=notifier,
        market_state_detector=mstate,
        state_bridge=state_bridge,
        # broker adapter は渡さない (shadow 境界, §7.3)。
    )
    logger.info(
        "[ORCH] runtime built (mode=%s, pairs=%s, shadow_notify=%s)",
        orch_cfg.mode, pairs, orch_cfg.notifications.shadow_enabled,
    )
    return runtime


def _build_detector(
    config: "AppConfig", orch_cfg, analysis_store: "AnalysisStore",
    pairs: list[str], *, store: "VectorStore",
) -> MaterialLandingDetector:
    """planning 発火フィルタ。technical bias + news/event landing を引く (§5.4①)。

    technical bias は AnalysisStore、news 経路は RAG 集計、event 経路は EconEventStore の
    高重要度イベント window から判定する (Phase1 A-2 で配線)。provider 構築に失敗した経路は
    None のままにし、detector 側の既定 (常に False) に安全に倒す (technical 経路は不変)。
    """
    def get_latest_technical(pair: str):
        snaps = analysis_store.get_recent_ok_snapshots(pair)
        return snaps[0] if snaps else None

    # news / event provider を配線する (§5.4①)。構築失敗は技術経路だけで動かす。
    get_news_impact = get_news_key = None
    in_event_window = get_event_key = None
    try:
        from src.orchestrator.landing_providers import (
            make_event_window_provider,
            make_news_material_provider,
        )

        get_news_impact, get_news_key = make_news_material_provider(config, store)
        from src.data.econ_event_store import EconEventStore

        econ_store = EconEventStore(config.prices_db_path)
        in_event_window, get_event_key = make_event_window_provider(config, econ_store)
    except Exception:
        logger.warning(
            "[ORCH] news/event landing provider 構築に失敗 — technical 経路のみで発火",
            exc_info=True,
        )

    return MaterialLandingDetector(
        get_latest_technical=get_latest_technical,
        material_bias_delta_min=orch_cfg.firing.material_bias_delta_min,
        get_news_impact=get_news_impact,
        material_news_impact_min=orch_cfg.firing.material_news_impact_min,
        get_news_key=get_news_key,
        in_event_window=in_event_window,
        get_event_key=get_event_key,
        debounce_window_seconds=orch_cfg.firing.debounce_window_seconds,
        min_planning_interval_seconds=orch_cfg.firing.min_planning_interval_seconds,
        pairs=pairs,
    )


def _build_market_state(config: "AppConfig", orch_cfg, pairs: list[str]):
    """market state 検知器 + bridge を組む (Phase1 Task C-4)。

    `orchestrator.market_state_enabled` が false なら (None, None) を返し state ループを
    起動しない。enabled 時は horizon overlay 済み detector と、regime 変化を log する bridge
    を返す。

    **cadence boost (経路②) の resolver 接続について:** cadence_driver (main.py) と
    orchestrator runtime (本 bootstrap) は別経路で resolver を共有しないため、ここでは
    resolver=None の縮退モード (regime コールバックのみ) で bridge を組む。cadence と
    runtime が同一 resolver を共有する構成は後続の統合作業 (本 Phase の shadow 範囲外)。
    """
    if not orch_cfg.market_state_enabled:
        return None, None
    from src.orchestrator.cadence_sources import MarketStateBridge
    from src.orchestrator.market_state_detector import detector_with_horizon

    detector = detector_with_horizon(orch_cfg.market_state, orch_cfg.policy.trade_horizon)

    def _on_regime(pair: str, state: str) -> None:
        # regime 変化を log。planning 再計画への接続は material landing 経由 (§5.4①) に
        # 委ねる前提で、ここでは観測ログに留める (執行は制御しない §5.2)。
        logger.info("[ORCH] regime change %s → %s", pair, state)

    bridge = MarketStateBridge(
        resolver=None,
        boost_interval_sec=config.schedule.cadence_boost_interval_minutes * 60,
        boost_ttl_sec=config.orchestrator.market_state.active_seconds * 4,
        on_regime_change=_on_regime,
    )
    logger.info("[ORCH] market state detection enabled (horizon=%s)",
                orch_cfg.policy.trade_horizon)
    return detector, bridge


def _build_pipeline(config: "AppConfig", orch_store: "OrchestratorStore"):
    """planning loop の LLM パイプラインを組む。

    LLM クライアントは既存 factory (price_analysis ロール) を流用。PlannerAgent /
    ExecutionOpinionAgent は同一 LLM を共有する (逐次・worker=1, §4.2)。RiskGateWorker は
    spread 閾値のみ config から取る (pre-check, shadow は hard veto しない)。
    """
    from src.llm.factory import create_llm_client
    from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
    from src.orchestrator.planner_agent import PlannerAgent
    from src.orchestrator.planning_pipeline import PlanningPipeline
    from src.orchestrator.risk_gate import RiskGateWorker

    llm = create_llm_client(config, "price_analysis")
    return PlanningPipeline(
        orch_store=orch_store,
        planner=PlannerAgent(llm),
        execution_agent=ExecutionOpinionAgent(llm),
        risk_gate=RiskGateWorker(
            spread_max_pips=config.orchestrator.entry.spread_max_pips
        ),
        config=config.orchestrator,
    )
