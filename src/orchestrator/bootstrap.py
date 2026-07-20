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
  - create_live_notifier(notifier cfg)       — live 発注結果の通知 (通常 webhook)
  - LlmDispatcher + PlanningPipeline         — planning loop の LLM 逐次パイプライン
  - MaterialLandingDetector                  — planning 発火フィルタ (material + debounce)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from src.orchestrator.context_builder import (
    DecisionContextBuilder,
    QuoteSnapshot,
    make_cached_news_provider,
    make_news_provider,
)
from src.orchestrator.hindsight_evaluator import make_hindsight_evaluator
from src.orchestrator.material_landing import MaterialLandingDetector
from src.orchestrator.runtime import OrchestratorRuntime, QuoteProvider
from src.orchestrator.live_notifier import create_live_notifier
from src.orchestrator.shadow_notifier import create_shadow_notifier
from src.trading.market_state import market_skip_check
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.config.schema import AppConfig
    from src.data.analysis_store import AnalysisStore
    from src.data.orchestrator_store import OrchestratorStore
    from src.data.price_provider import PriceProvider
    from src.data.price_store import PriceStore
    from src.orchestrator.cadence_resolver import CadenceResolver
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


def make_position_provider(config: "AppConfig") -> Callable[[str], list[dict]]:
    """planning context 用の raw position provider (spec P-1)。

    ProtectionWorker と同じく self-contained な PositionManager を1つ持ち、
    呼び出し毎に disk から reload する (planning は 60s 周期なので安価)。
    整形 (pnl_r/long 正規化) は DecisionContextBuilder 側 (codex High#2)。
    順序は get_open_positions_by_pair の opened_at 昇順 (時系列で LLM に渡す)。
    """
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager

    mgr = PositionManager(StateStore(config.state_dir), context="PlanningContext")

    def provider(pair: str) -> list[dict]:
        mgr.reload()
        out: list[dict] = []
        for o in mgr.get_open_positions_by_pair(pair):
            out.append({
                "direction": o.direction,                     # "buy" | "sell" raw
                "entry_price": o.entry_price,
                "size": o.position_size,
                "opened_at": o.opened_at.isoformat() if o.opened_at else None,
                "mfe_r": o.max_favorable_r,
                "initial_risk_price_distance": o.initial_risk_price_distance,
                "is_scale_in": o.is_scale_in,
                "entry_reason": o.signal_reason or "",
            })
        return out

    return provider


def make_producer_quote_provider(producer, fallback: QuoteProvider) -> QuoteProvider:
    """producer.latest を読む quote provider。latest が None (起動直後の過渡期) のみ
    従来 fetch にフォールバックする (spec §4.4)。"""

    def provider(pair: str) -> QuoteSnapshot:
        snap = producer.latest(pair)
        if snap is None:
            return fallback(pair)
        return snap

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
    cadence_resolver: "CadenceResolver | None" = None,
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

    # 前プロセスの dangling run を回収する (spec §3.6)。異常終了で finished_at IS NULL の
    # まま残った run は has_running_planning_run の stale 閾値で除外されるが、起動時に
    # 明示的に failed/dangling として畳んでおく。finish_dangling_runs 自身も同じ stale
    # 閾値を持つので、多重起動時に他プロセスの実行中 run を巻き込まない。
    recovered = orch_store.finish_dangling_runs(now=db_now())
    if recovered:
        logger.warning("[ORCH] recovered %d dangling agent runs", recovered)

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
            # live では黙って drop してはならない (spec §1.5 / 実装後レビュー H-2)。
            # drop すると残った pairs が tradeable 全体と一致してしまい、下の pair
            # 整合チェックを素通りして起動が成功する。運用者は drop された pair も
            # 発注していると信じるが発注経路は存在しない = §1.5 が潰そうとしている
            # 「無音の発注経路欠落」そのもの。
            if orch_cfg.mode == "live":
                raise RuntimeError(
                    f"[ORCH] orchestrator.pairs に tradeable でない symbol が"
                    f"含まれています: {dropped} (live mode)。これらの発注経路は"
                    f"存在しません。instruments 側で mode: trade にするか、"
                    f"orchestrator.pairs から外してください。"
                )
            logger.warning(
                "[ORCH] orchestrator.pairs に tradeable でない symbol が含まれ除外: %s",
                dropped,
            )
    else:
        pairs = tradeable

    # 重複 pair (外部レビュー Medium)。下の整合チェックは set 比較なので重複を
    # 素通りするが、pairs は重複 list のまま runtime に渡り material_landing 等で
    # 同一 pair が複数回処理される (LLM 計画の重複・plan の不要な supersede・
    # 余分な watch)。重複は明らかに config のミスなので、非 tradeable symbol の
    # 扱い (H-2) と同じ方針で live は起動中止・非 live は warning + 続行にする。
    if len(set(pairs)) != len(pairs):
        dupes = sorted({s for s in pairs if pairs.count(s) > 1})
        if orch_cfg.mode == "live":
            raise RuntimeError(
                f"[ORCH] orchestrator の対象 pair に重複があります: {dupes} "
                f"(live mode)。同一 pair が多重に planning/watch され、計画重複や "
                f"plan の不要な supersede を招きます。orchestrator.pairs / "
                f"instruments の設定を修正してください。"
            )
        logger.warning(
            "[ORCH] 対象 pair に重複があるため除去しました (%s mode): %s",
            orch_cfg.mode, dupes,
        )
        # 順序は config の初出順を維持する (dedup で並べ替えない)。
        pairs = list(dict.fromkeys(pairs))

    if not pairs:
        logger.warning(
            "[ORCH] no tradeable pairs to orchestrate (configured=%s) — runtime not built",
            configured or "<all tradeable>",
        )
        return None

    # pair 集合の整合 (spec §1.5): orchestrator 対象 pair が tradeable 全体と一致
    # しない場合、mode=live では対象外 pair の発注経路が存在しない状態になるため
    # 起動中止する。subset 運用は tradeable 側の設定で表現する (許可フラグは設けない)。
    tradeable_all = set(tradeable)
    selected = set(pairs)
    if selected != tradeable_all:
        missing = sorted(tradeable_all - selected)
        if orch_cfg.mode == "live":
            raise RuntimeError(
                f"[ORCH] pair set mismatch in live mode: {missing} have no "
                f"ordering path (spec §1.5). Fix orchestrator.pairs or "
                f"instruments config."
            )
        logger.warning(
            "[ORCH] pair subset in %s mode: %s not covered (allowed outside live)",
            orch_cfg.mode, missing,
        )

    # protection_pairs は planning scope (pairs) と分離する (Codex High)。pairs は
    # orchestrator.pairs で subset に絞れる「能動的に plan/trigger する」スコープだが、
    # protect_live では price_monitor の利益保護がペア無関係に OFF になるため
    # (price_monitor.py:87 の execute は global)、保護スコープは「ポジションを持ち得る
    # 全ペア」= tradeable 全体でなければならない。さもないと subset 外の tradeable ペア
    # (例: orchestrator.pairs=USDJPY のときの EURUSD) で利益保護が完全に消える。
    # よって producer / 保護 worker は protection_pairs (= tradeable 全体) をカバーする。
    # instruments 側に重複 symbol があっても producer / 保護 worker は 1 回だけ。
    protection_pairs = list(dict.fromkeys(tradeable))

    context_builder = DecisionContextBuilder(
        orch_store, analysis_store, orch_cfg,
        news_provider=make_cached_news_provider(
            make_news_provider(config, store),
            ttl_seconds=orch_cfg.entry.news_cache_ttl_seconds,
            negative_ttl_seconds=orch_cfg.entry.news_cache_negative_ttl_seconds,
            clock=db_now,
        ),
        risk_state_provider=make_risk_state_provider(config),
        position_provider=make_position_provider(config),
    )

    quote_provider = make_quote_provider(price_provider)

    # Phase 2/D: tick_migration_stage が producer 以上なら quote-stream producer を立て
    # watch を producer 直読に切り替える (spec §4.4)。off は従来 fetch 維持。
    quote_producer = None
    stage = getattr(orch_cfg, "tick_migration_stage", "off")
    if stage in ("producer", "protect_shadow", "protect_live"):
        from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher
        from src.data.quote_stream import QuoteStreamProducer

        # review M-d: price_provider の内部 fetcher は api_key 無しで生成される
        # (price_provider.py:71-74)。producer 用に config から api_key 付きで作り直す。
        # bridge 認証有効環境で /quote が 401 → 常時 degrade するのを防ぐ。
        mt5_enabled = getattr(price_provider, "_mt5_enabled", False)
        mt5_cfg = config.providers.mt5
        quote_fetcher = None
        if mt5_enabled and mt5_cfg is not None:
            quote_fetcher = Mt5OhlcvFetcher(
                bridge_url=mt5_cfg.bridge_url,
                request_timeout=mt5_cfg.request_timeout_seconds,
                api_key=getattr(mt5_cfg, "api_key", "") or "",
            )
        # producer は protection_pairs (= tradeable 全体) を poll する。planning が使う
        # のは subset の pairs だけだが、producer.latest は watch が問い合わせたペアしか
        # 引かない (make_producer_quote_provider) ので、広い producer は planning に無害
        # (余分なペアは poll されるが planning では未使用)。一方 protect_live の保護 worker
        # は全 tradeable ペアの latest を必要とするため、広いカバレッジが必須 (Codex High)。
        quote_producer = QuoteStreamProducer(
            pairs=protection_pairs, fetcher=quote_fetcher, price_provider=price_provider,
            mt5_enabled=mt5_enabled,
            poll_seconds=getattr(orch_cfg, "quote_stream_poll_seconds", 2),
        )
        quote_provider = make_producer_quote_provider(quote_producer, quote_provider)

    # protect_shadow 以上では tick 駆動の保護 worker を立てる (spec §5.2)。producer 直読の
    # mid で MFE/R protection を判定し protection_decisions に記録する。protect_shadow は
    # execute=False なので副作用なし、protect_live で初めて remote SL 適用 + state 更新が発火。
    protection_worker = None
    if stage in ("protect_shadow", "protect_live") and quote_producer is not None:
        from src.orchestrator.position_protection_worker import PriceProtectionWorker
        from src.persistence.state_store import StateStore
        from src.trading.live_broker import build_close_broker
        from src.trading.position_manager import PositionManager

        # price_monitor (run_price_monitor) と同じく self-contained に PositionManager /
        # close broker を構築する: PositionManager(StateStore(config.state_dir), context=...)、
        # broker は build_close_broker(config)。worker 専用 instance なので trading cycle と
        # 干渉しない (state は disk 共有・mutation 直前に reload される)。
        prot_position_mgr = PositionManager(
            StateStore(config.state_dir), context="ProtectionWorker"
        )
        prot_broker = build_close_broker(config)

        # 保護 worker は bootstrap で一度だけ作る長命 instance なので、tick 毎に disk から
        # reload してから positions を読む (Codex High)。さもないと daemon 起動後に trade
        # cycle が別 PositionManager 経由で建てた新規ポジションが in-memory _open に反映
        # されず、protect_live でその新規ポジションの利益保護が完全に欠落する (price_monitor
        # も protect_live では OFF)。reload は positions.json/balance.json 読みのみで安価、
        # poll cadence (quote_stream_poll_seconds, 既定2s) で走る。StateStore の RLock は
        # 同一プロセス内 state_dir 単位なので trade cycle の書き込みと整合する。
        def _fresh_open_positions():
            prot_position_mgr.reload()
            return prot_position_mgr.get_account_state().open_positions

        protection_worker = PriceProtectionWorker(
            producer=quote_producer,
            position_provider=_fresh_open_positions,
            store=orch_store, cfg=config.trading,
            position_mgr=prot_position_mgr, broker=prot_broker,
            mode=stage,
            remote_sync_enabled=getattr(config.trading, "remote_sl_sync_enabled", False),
            poll_seconds=getattr(orch_cfg, "quote_stream_poll_seconds", 2),
        )

    detector = _build_detector(config, orch_cfg, analysis_store, pairs, store=store)
    # risk gate は単一構築で pipeline (planning) と runtime (live final gate /
    # shadow precheck) に共有する — 二重構築だと config (min_rr 等) が runtime 側
    # fallback に届かず、planning と live で閾値がズレる (spec 2026-07-16 §2.E)。
    from src.orchestrator.risk_gate import RiskGateWorker
    risk_gate = RiskGateWorker(
        min_rr=config.orchestrator.entry.min_rr,
        spread_max_pips=config.orchestrator.entry.spread_max_pips,
    )
    pipeline = _build_pipeline(config, orch_store, risk_gate)
    hindsight = make_hindsight_evaluator(price_store, interval=config.trading.ohlcv_interval)
    notifier = create_shadow_notifier(orch_cfg.notifications)
    # live 発注結果は shadow とは別経路で通知する (レビュー High)。制御は通常の
    # notification.enabled + DISCORD_WEBHOOK_URL で、shadow フラグ・shadow webhook
    # からは完全に独立。
    live_notifier = create_live_notifier(config.notifier)

    mstate, state_bridge = _build_market_state(
        config, orch_cfg, pairs,
        cadence_resolver=cadence_resolver, landing_detector=detector,
    )

    # Task F (F-4): orchestrator.mode=live なら発注用 execution broker + position_mgr を
    # 構築し執行段に注入する (single execution writer)。shadow では None で live 分岐に
    # 入らない (回帰維持・shadow 境界)。
    execution_broker = None
    execution_position_mgr = None
    if orch_cfg.mode == "live":
        execution_broker, execution_position_mgr = _build_execution_broker(config)

    runtime = OrchestratorRuntime(
        config=orch_cfg,
        orch_store=orch_store,
        context_builder=context_builder,
        pairs=pairs,
        quote_provider=quote_provider,
        quote_producer=quote_producer,
        protection_worker=protection_worker,
        detector=detector,
        pipeline=pipeline,
        risk_gate=risk_gate,
        hindsight_evaluator=hindsight,
        shadow_notifier=notifier,
        live_notifier=live_notifier,
        market_state_detector=mstate,
        state_bridge=state_bridge,
        # Task F: mode=live のとき発注 broker / position_mgr / app_config を執行段へ注入。
        # shadow では None で live 分岐に入らない (§7.3 shadow 境界維持)。
        mode=orch_cfg.mode,
        execution_broker=execution_broker,
        execution_position_mgr=execution_position_mgr,
        app_config=config,
    )
    logger.info(
        "[ORCH] runtime built (mode=%s, pairs=%s, shadow_notify=%s)",
        orch_cfg.mode, pairs, orch_cfg.notifications.shadow_enabled,
    )

    # Task F (spec §3): 起動時に前回クラッシュの未完了 order_intent を recovery 分類する。
    # shadow でも実行して害は無い (order_intents が空なら no-op)。
    from src.orchestrator.order_recovery import recover_pending_intents
    recover_pending_intents(orch_store, now=db_now())

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

        get_news_impact, get_news_key = make_news_material_provider(
            config, store,
            ttl_seconds=orch_cfg.entry.news_cache_ttl_seconds,
            negative_ttl_seconds=orch_cfg.entry.news_cache_negative_ttl_seconds,
            clock=db_now,
        )
        from src.data.econ_event_store import EconEventStore

        econ_store = EconEventStore(config.econ_db_path)
        in_event_window, get_event_key = make_event_window_provider(config, econ_store)
    except Exception:
        logger.warning(
            "[ORCH] news/event landing provider 構築に失敗 — technical 経路のみで発火",
            exc_info=True,
        )

    return MaterialLandingDetector(
        get_latest_technical=get_latest_technical,
        material_bias_delta_min=orch_cfg.firing.material_bias_delta_min,
        material_direction_flip_min=orch_cfg.firing.material_direction_flip_min,
        get_news_impact=get_news_impact,
        material_news_impact_min=orch_cfg.firing.material_news_impact_min,
        get_news_key=get_news_key,
        in_event_window=in_event_window,
        get_event_key=get_event_key,
        debounce_window_seconds=orch_cfg.firing.debounce_window_seconds,
        min_planning_interval_seconds=orch_cfg.firing.min_planning_interval_seconds,
        pairs=pairs,
    )


def _build_market_state(
    config: "AppConfig", orch_cfg, pairs: list[str],
    *, cadence_resolver: "CadenceResolver | None" = None,
    landing_detector: "MaterialLandingDetector | None" = None,
):
    """market state 検知器 + bridge を組む (Phase1 Task C-4 / code review High#2)。

    `orchestrator.market_state_enabled` が false なら (None, None) を返し state ループを
    起動しない。enabled 時は horizon overlay 済み detector と bridge を返す。

    **cadence boost (経路②) の resolver 接続:** main._build_cadence_driver() が生成した
    `CadenceResolver` を `cadence_resolver` で受け取り bridge に渡す。これにより market state
    loop の state boost が実収集 interval に反映される (High#2)。`cadence_enabled` が off で
    resolver=None の場合は縮退モード (regime コールバックのみ・boost は書かない)。
    """
    if not orch_cfg.market_state_enabled:
        return None, None
    from src.orchestrator.cadence_sources import MarketStateBridge
    from src.orchestrator.market_state_detector import detector_with_horizon

    detector = detector_with_horizon(orch_cfg.market_state, orch_cfg.policy.trade_horizon)

    def _on_regime(pair: str, state: str) -> None:
        # regime 変化を material landing detector に push する (§5.4① / Task C-3)。
        # detector が次の planning 周期で material として拾い、debounce/floor/consumed-key の
        # 既存機構経由で再計画する (執行は制御しない §5.2)。detector 未注入なら log のみ。
        logger.info("[ORCH] regime change %s → %s", pair, state)
        if landing_detector is not None:
            landing_detector.mark_regime(pair, state)

    bridge = MarketStateBridge(
        resolver=cadence_resolver,  # cadence_enabled 時のみ非None (boost を実反映)
        boost_interval_sec=config.schedule.cadence_boost_interval_minutes * 60,
        boost_ttl_sec=config.orchestrator.market_state.active_seconds * 4,
        on_regime_change=_on_regime,
    )
    _connected = "resolver-shared" if cadence_resolver is not None else "regime-only"
    logger.info("[ORCH] market state detection enabled (horizon=%s, %s)",
                orch_cfg.policy.trade_horizon, _connected)
    return detector, bridge


def _build_pipeline(config: "AppConfig", orch_store: "OrchestratorStore", risk_gate):
    """planning loop の LLM パイプラインを組む。

    PlannerAgent / ExecutionOpinionAgent はそれぞれ独立した AgentLlm
    (client + temperature) を受け取る (per-agent llm config)。llm.yaml に
    設定が無ければ両者とも price_analysis 役割 client + 役割 temperature に
    fallback し従来動作 (逐次・worker=1, §4.2)。

    RiskGateWorker は build_orchestrator_runtime が単一構築したものを受け取る —
    pipeline (planning) と runtime (live final gate / shadow precheck) で閾値が
    ズレないよう同一インスタンスを共有する (spec 2026-07-16 §2.E)。
    """
    from src.llm.factory import create_agent_llm
    from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
    from src.orchestrator.planner_agent import PlannerAgent
    from src.orchestrator.planning_pipeline import PlanningPipeline

    planner_llm = create_agent_llm(config, "planner")
    exec_llm = create_agent_llm(config, "execution_opinion")
    return PlanningPipeline(
        orch_store=orch_store,
        planner=PlannerAgent(planner_llm, user_notes_path=config.user_notes_path),
        execution_agent=ExecutionOpinionAgent(exec_llm),
        risk_gate=risk_gate,
        config=config.orchestrator,
    )


def _build_execution_broker(config: "AppConfig"):
    """Task F (F-4): orchestrator 執行段の発注 broker + position_mgr を構築する。

    broker は trading cycle と同じ `create_broker(mode=AppConfig.mode, ...)` で組む
    (src/cycles/trading.py:1037 と同一引数)。発注先は top-level AppConfig.mode が決める:
    paper→PaperBroker (仮想資金), live_test→paper+MT5 observer, live→本番 broker。

    codex Critical: live_test は ShadowBrokerAdapter の observer (MT5) が実 execute_signal
    するため、bridge が DRY_RUN=false だと MT5 order_send に到達する。config では bridge の
    runtime 状態を検証できないので、ここで bridge /health の dry_run==true を確認し、
    true でなければ起動を弾く (実発注事故の防止)。paper は MT5 に到達しないため gate 不要。
    """
    from src.notifications.notifier import create_notifier
    from src.persistence.state_store import StateStore
    from src.trading.live_broker import create_broker
    from src.trading.position_manager import PositionManager

    mt5_cfg = config.providers.mt5

    # codex Critical: live_test は MT5 observer が実発注しうる。bridge の dry_run を確認し、
    # true でなければ起動を弾く (broker 構築前に gate)。
    if config.mode == "live_test":
        _assert_bridge_dry_run(config)

    notifier = create_notifier(config.notifier.enabled)
    broker = create_broker(
        config.mode,
        config.live_broker,
        max_positions_per_pair=config.trading.max_positions_per_pair,
        scale_in_enabled=config.trading.scale_in_enabled,
        scale_in_conf_margin=config.trading.scale_in_conf_margin,
        scale_in_score_margin=config.trading.scale_in_score_margin,
        notifier=notifier,
        state_dir=config.state_dir,
        drawdown_kill_switch_enabled=config.trading.drawdown_kill_switch_enabled,
        drawdown_kill_switch_max_pct=config.trading.drawdown_kill_switch_max_pct,
        mt5_bridge_url=(mt5_cfg.bridge_url if mt5_cfg else ""),
        mt5_lot_size_units=(mt5_cfg.lot_size_units if mt5_cfg else 100_000),
        mt5_magic_number=(mt5_cfg.magic_number if mt5_cfg else 12345),
        mt5_order_timeout_seconds=(mt5_cfg.order_request_timeout_seconds if mt5_cfg else 10.0),
        mt5_consecutive_reject_threshold=(mt5_cfg.consecutive_reject_threshold if mt5_cfg else 3),
        live_test_log_path=(mt5_cfg.shadow_log_path if mt5_cfg else "data/state/shadow_trades.jsonl"),
        live_test_observer_state_dir=(mt5_cfg.shadow_observer_state_dir if mt5_cfg else "data/shadow_state"),
    )
    position_mgr = PositionManager(
        StateStore(config.state_dir), context="OrchestratorExecution"
    )
    logger.info(
        "[ORCH] execution broker built (AppConfig.mode=%s, live_broker=%s)",
        config.mode, config.live_broker,
    )
    return broker, position_mgr


def _assert_bridge_dry_run(config: "AppConfig") -> None:
    """live_test 起動前に MT5 bridge の /health dry_run==true を要求する (codex Critical)。

    dry_run==false (= 実発注モード) または health 取得失敗 (接続不可・タイムアウト・
    形不正) のときは安全側で ValueError を投げ、live_test の実発注事故を防ぐ。
    """
    import httpx

    mt5_cfg = config.providers.mt5
    bridge_url = (mt5_cfg.bridge_url if mt5_cfg else "").rstrip("/")
    if not bridge_url:
        raise ValueError(
            "orchestrator.mode=live + AppConfig.mode=live_test requires a configured "
            "mt5 bridge_url to verify dry_run state; got empty bridge_url."
        )
    headers = {}
    if mt5_cfg and mt5_cfg.api_key:
        headers["X-Bridge-Api-Key"] = mt5_cfg.api_key
    try:
        resp = httpx.get(f"{bridge_url}/health", headers=headers, timeout=5.0)
        resp.raise_for_status()
        dry_run = resp.json().get("dry_run")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"orchestrator.mode=live + AppConfig.mode=live_test: could not verify "
            f"MT5 bridge dry_run state at {bridge_url}/health ({exc}); refusing to "
            "start to avoid accidental live orders. Use AppConfig.mode=paper for the "
            "safest dry-run validation (never reaches MT5)."
        ) from exc
    if dry_run is not True:
        raise ValueError(
            f"orchestrator.mode=live + AppConfig.mode=live_test requires the MT5 bridge "
            f"to run with DRY_RUN=true; bridge reports dry_run={dry_run!r}. Refusing to "
            "start to avoid live orders via the MT5 observer."
        )
