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

    CurrentPrice は bid/ask を持たず price のみ (yfinance/TD/MT5 共通形)。Phase 6 では
    bid=ask=mid=price / spread=0 とする (実 spread の tick 受信は websocket 化の後続
    フェーズ)。spread=0 は spread gate を必ず通すが、shadow は発注しないため許容。
    """

    def provider(pair: str) -> QuoteSnapshot:
        cp = price_provider.get_current_price(pair)
        # observed_at は DB 規約 (db_now = naive ローカル) に揃える。CurrentPrice.timestamp
        # が無ければ now で補完する。context_builder は isoformat して snapshot に保存する。
        observed = cp.timestamp or db_now()
        return QuoteSnapshot(
            bid=cp.price, ask=cp.price, mid=cp.price, spread=0.0,
            source=cp.source, observed_at=observed,
        )

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

    # pairs は tradeable のみ (watch は planning/trigger 対象外、§4.8)。
    pairs = [inst.symbol for inst in config.tradeable_instruments]
    if not pairs:
        logger.warning("[ORCH] no tradeable instruments — runtime not built")
        return None

    context_builder = DecisionContextBuilder(
        orch_store, analysis_store, orch_cfg,
        news_provider=make_news_provider(config, store),
    )

    quote_provider = make_quote_provider(price_provider)

    detector = _build_detector(orch_cfg, analysis_store, pairs)
    pipeline = _build_pipeline(config, orch_store)
    hindsight = make_hindsight_evaluator(price_store)
    notifier = create_shadow_notifier(orch_cfg.notifications)

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
        # broker adapter は渡さない (shadow 境界, §7.3)。
    )
    logger.info(
        "[ORCH] runtime built (mode=%s, pairs=%s, shadow_notify=%s)",
        orch_cfg.mode, pairs, orch_cfg.notifications.shadow_enabled,
    )
    return runtime


def _build_detector(
    orch_cfg, analysis_store: "AnalysisStore", pairs: list[str]
) -> MaterialLandingDetector:
    """planning 発火フィルタ。technical bias を AnalysisStore から引く。

    news/event 経路は Phase 6 では未配線 (technical bias delta + periodic floor のみで
    発火)。news_material / event_window は callable 未注入で常に False に倒れる
    (detector の既定挙動)。これらの配線は §5.3/§5.4 の cadence/econ 連携の後続作業。
    """
    def get_latest_technical(pair: str):
        snaps = analysis_store.get_recent_ok_snapshots(pair)
        return snaps[0] if snaps else None

    return MaterialLandingDetector(
        get_latest_technical=get_latest_technical,
        material_bias_delta_min=orch_cfg.firing.material_bias_delta_min,
        material_news_impact_min=orch_cfg.firing.material_news_impact_min,
        debounce_window_seconds=orch_cfg.firing.debounce_window_seconds,
        min_planning_interval_seconds=orch_cfg.firing.min_planning_interval_seconds,
        pairs=pairs,
    )


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
