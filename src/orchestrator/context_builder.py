"""Context Builder (spec §7 / §8.7)。

decision 開始時に decision_snapshot を materialize し、§7 の標準 context dict を
SQL から決定的に組む (LLM なし)。同一 decision 内の全 agent はこの snapshot
経由でのみ quote/technical/news を読む。

Phase 1: technical は AnalysisStore から、quote は呼び出し側から受け取る。
news / similar_cases / recent_trade_stats は後続 plan で埋める (今は空/既定)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore

if TYPE_CHECKING:
    from src.config.schema import AppConfig
    from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# pair -> §7 news ブロック ({"sentiment_score", "confidence", "top_reasons"})。
# 実装は RAG 集計 (aggregate_news_sentiment) のアダプタ。detector と同じ後方互換の
# オプション注入パターンで、None なら従来の空 news に倒す。
NewsProvider = Callable[[str], dict[str, Any]]

# pair -> §7 risk_state ブロック ({"halt", "bridge_health", "market_open", "cooldown"})。
# market_skip_check / halt_state / bridge health を集約するアダプタ。None なら従来の
# 楽観既定 (_empty_risk_state) に倒す (後方互換)。production では必ず注入する
# (未注入だと休場/halt 中でも gate が素通りする — Codex High)。
RiskStateProvider = Callable[[str], dict[str, Any]]

# pair -> raw open positions (bootstrap.make_position_provider が生成)。
# 整形 (buy→long / pnl_r) は builder 側 (spec P-1, codex High#2)。
PositionProvider = Callable[[str], list[dict]]

# provider 失敗時に倒す安全側 risk_state。gate 状態が読めない以上、楽観既定 (market_open=
# True) ではなく執行を止める方向に倒す (休場/halt 中の誤 trigger を防ぐ)。
_SAFE_RISK_STATE = {
    "halt": "soft", "bridge_health": "unknown", "market_open": False, "cooldown": True,
}


@dataclass
class QuoteSnapshot:
    """watch 側から渡される瞬間 quote (planning は snapshot 経由で読む)。"""
    bid: float
    ask: float
    mid: float
    spread: float | None   # None = 実 spread 不明 (bid/ask 無し provider)。gate は安全側に倒す
    source: str
    observed_at: datetime


class DecisionContextBuilder:
    """decision_snapshot を作り §7 標準 context を組む層 (決定的)。

    名前について: 「decision の入力 context を組む」役割を明示する。views.py の
    AskContextBuilder (RAG 用) や trading の entry_context_builder (別概念) との
    混同を避けるため Decision 接頭辞を付ける (用途接頭辞の既存命名規則に倣う)。
    """

    def __init__(
        self,
        orch_store: OrchestratorStore,
        analysis_store: AnalysisStore,
        config: OrchestratorConfig,
        news_provider: NewsProvider | None = None,
        risk_state_provider: "RiskStateProvider | None" = None,
        position_provider: "PositionProvider | None" = None,
    ) -> None:
        self._orch = orch_store
        self._analysis = analysis_store
        self._config = config
        self._news_provider = news_provider
        self._risk_state_provider = risk_state_provider
        self._position_provider = position_provider

    # technical snapshot の stale 閾値は orchestrator.entry.max_technical_age_seconds
    # から導出する (watch loop の freshness gate と同一基準に統一 — codex Med#2)。
    # lookback 窓は「窓 > max_stale」を機械的に保証する従来比率 (×48/60) を維持する。

    @property
    def _technical_max_stale(self) -> timedelta:
        return timedelta(seconds=self._config.entry.max_technical_age_seconds)

    @property
    def _technical_lookback_hours(self) -> int:
        # lookback 窓。stale と missing を区別するため max_stale より十分広く取る
        # 必要がある (窓が max_stale と同じだと、max_stale 超過の行が窓から外れて
        # missing に見え、stale と区別できない)。max_stale から導出し「窓 > max_stale」
        # を機械的に保証する (将来 max_stale を変えても窓が自動追従する)。最低 1h。
        minutes = self._config.entry.max_technical_age_seconds // 60
        return max(1, (minutes * 48) // 60)

    def build(self, *, pair: str, now: datetime, quote: QuoteSnapshot) -> dict[str, Any]:
        """decision_snapshot を materialize し §7 標準 context dict を返す。"""
        technical = self._build_technical(pair, now)
        news = self._build_news(pair, now)
        quote_dict = self._quote_dict(quote)
        position = self._build_position(pair, quote_dict)
        current_plan = self._build_current_plan(pair)

        snapshot_id = self._orch.create_snapshot(
            pair=pair,
            as_of_time=now,
            quote_json=quote_dict,
            technical_ref=technical.get("_ref"),
            news_ref=news.get("_ref"),
            position_json=position,
            current_plan_json=current_plan,
        )
        ctx = self._assemble(pair, now, quote_dict, technical, news,
                             position=position, current_plan=current_plan)
        ctx["snapshot_id"] = snapshot_id
        return ctx

    def assemble(self, *, pair: str, now: datetime, quote: QuoteSnapshot) -> dict[str, Any]:
        """snapshot を作らずに §7 標準 context dict を返す (watch loop の条件評価用)。

        watch loop は 1s polling で active plan を tick 評価する。毎 tick で snapshot を
        materialize すると無駄が大きいため、trigger 確定前の entry/invalidation/freshness
        評価にはこの非永続版を使う (§7.1: 新規 snapshot は trigger 確定時のみ作る)。
        `snapshot_id` キーは含まない (build() のみが付与する)。
        """
        technical = self._build_technical(pair, now)
        news = self._build_news(pair, now)
        return self._assemble(pair, now, self._quote_dict(quote), technical, news)

    @staticmethod
    def _quote_dict(quote: QuoteSnapshot) -> dict[str, Any]:
        return {
            "bid": quote.bid, "ask": quote.ask, "mid": quote.mid,
            "spread": quote.spread, "source": quote.source,
            "observed_at": quote.observed_at.isoformat(),
        }

    def _assemble(
        self,
        pair: str,
        now: datetime,
        quote_dict: dict[str, Any],
        technical: dict[str, Any],
        news: dict[str, Any],
        position: dict[str, Any] | None = None,
        current_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """technical/news/quote から §7 標準 context dict を組む (snapshot_id 無し)。"""
        return {
            "pair": pair,
            "now": now.isoformat(),
            "quote": quote_dict,
            "position": position if position is not None else self._empty_position(),
            "current_plan": current_plan,
            "technical": {k: v for k, v in technical.items() if k != "_ref"},
            "news": {k: v for k, v in news.items() if k != "_ref"},
            "risk_state": self._build_risk_state(pair),
            "data_health": {"issues": []},
            "recent_decisions": {"items": []},
            "recent_orders": {"items": []},
            "recent_exits": {"items": []},
            "recent_trade_stats": self._empty_trade_stats(),
            "move_maturity": self._empty_move_maturity(),
            "similar_cases": {"items": []},
            "policy": {
                "trade_horizon": self._config.policy.trade_horizon,
                "advice_memo": self._config.policy.advice_memo or None,  # "" -> None
                "plan_ttl_max_hours": self._config.plan_ttl_max_hours,
            },
        }

    def _build_technical(self, pair: str, now: datetime) -> dict[str, Any]:
        """AnalysisStore の lookback 内 ok snapshot から technical ブロックを組む。

        **重要 (設計思想):** `get_latest_ok_row` のような lookback 非依存読みは
        decision context に使わない (analysis_store.py が「取引判定からは絶対
        呼ばない」と明記。休場中の古い行で判断が汚染される)。代わりに
        `get_recent_ok_snapshots(hours=...)` で窓を切り、最新行の鮮度を `now` 基準で
        判定する。窓内に ok 行が無ければ missing、最新 ok が max_stale を超えていれば
        stale に倒す (どちらも direction/bias を判断材料に使わせない)。

        lookback 窓は max_stale より十分広く取る: max_stale を超えた行も「stale として
        観測」できる必要があるため (窓が max_stale と同じだと stale と missing を
        区別できない)。24h あれば数時間古い行も取得でき、age>max_stale で stale に
        分類できる。
        """
        max_stale = self._technical_max_stale
        rows = self._analysis.get_recent_ok_snapshots(
            pair, hours=self._technical_lookback_hours,
        )
        if not rows:
            return {
                "status": "missing", "bias_score": None, "confidence": None,
                "direction": None, "last_ok_at": None, "_ref": None,
            }

        row = rows[0]  # get_recent_ok_snapshots は新しい順
        # age が負 (row.analyzed_at > now) になりうる: now を DB 書き込み前に
        # 捕捉した場合など。負の age は age <= max_stale なので ok 扱い — 直近未来の
        # 行を fresh とみなすのは正しい。
        age = now - row.analyzed_at
        if age > max_stale:
            # 古すぎる: stale に倒し direction/bias は渡さない。参照だけ残す。
            return {
                "status": "stale", "bias_score": None, "confidence": None,
                "direction": None,
                "last_ok_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
                "_ref": {"snapshot_id": row.id, "analyzed_at": row.analyzed_at.isoformat()},
            }

        direction = (
            "long" if row.direction_bias == "long"
            else "short" if row.direction_bias == "short"
            else "neutral"
        )
        return {
            "status": "ok",
            "bias_score": row.bias_score,
            "confidence": row.confidence,
            "direction": direction,
            "last_ok_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
            "_ref": {"snapshot_id": row.id, "analyzed_at": row.analyzed_at.isoformat()},
        }

    def _build_news(self, pair: str, now: datetime) -> dict[str, Any]:
        """注入された news_provider から §7 news ブロックを組む。

        provider 未注入なら従来の空 news に倒す (後方互換)。provider が落ちても
        build 全体を止めない: news は判断材料の 1 つに過ぎず、1 材料の取得失敗で
        decision サイクル全体を落とすのは過剰 (technical の missing/stale と同じ思想)。
        失敗時は空 news + _ref なしに倒し、traceback を error ログに残す。

        `_ref` は build() が snapshot.news_ref に保存し (§8.1 trace)、返り値 context の
        news ブロックからは除去される (technical の _ref と同じ扱い)。
        """
        if self._news_provider is None:
            return {**self._empty_news(), "_ref": None}
        try:
            raw = self._news_provider(pair)
        except Exception:
            logger.exception("[ORCH] news_provider failed for %s — empty news", pair)
            return {**self._empty_news(), "_ref": None}
        return {
            "sentiment_score": raw.get("sentiment_score"),
            "confidence": raw.get("confidence"),
            "top_reasons": raw.get("top_reasons") or [],
            "_ref": {"source": "rag_aggregate", "as_of": now.isoformat()},
        }

    def _build_position(self, pair: str, quote_dict: dict[str, Any]) -> dict[str, Any]:
        """raw position を §7 position ブロックに整形する (P-1)。

        provider 失敗は status="unavailable" に倒す。このケースの安全は prompt
        でなく pipeline の決定的 fail-safe (P-4) が処理する。
        """
        if self._position_provider is None:
            return {"count": 0, "items": []}
        try:
            # 整形 loop も try 内: 壊れた raw dict (型不正など) で planning cycle を
            # 落とさず unavailable に倒す (Task 5 review Minor#1)。
            raw = self._position_provider(pair)
            mid = quote_dict.get("mid")
            items = [self._position_item(p, mid) for p in raw]
        except Exception:
            logger.warning(
                "[ORCH] position provider failed for %s — unavailable", pair,
                exc_info=True,
            )
            return {"count": None, "items": [], "status": "unavailable"}
        return {"count": len(items), "items": items}

    @staticmethod
    def _position_item(p: dict[str, Any], mid: float | None) -> dict[str, Any]:
        entry = p.get("entry_price")
        risk = p.get("initial_risk_price_distance") or 0.0
        pnl_r = None
        if mid is not None and entry is not None and risk > 0:
            delta = mid - entry
            if p.get("direction") == "sell":
                delta = -delta
            pnl_r = round(delta / risk, 2)
        return {
            "direction": "short" if p.get("direction") == "sell" else "long",
            "entry_price": entry,
            "size": p.get("size"),
            "opened_at": p.get("opened_at"),
            "pnl_r": pnl_r,
            "mfe_r": p.get("mfe_r"),
            "is_scale_in": bool(p.get("is_scale_in", False)),
            "entry_reason": (p.get("entry_reason") or "")[:200],
        }

    def _build_current_plan(self, pair: str) -> dict[str, Any] | None:
        """該当 pair の active plan (最大1件、supersede 保証) の要約 (P-2)。

        approval gate 導入時に対象へ pending_approval を追加する (gate spec F-1)。
        読み取り失敗は None (plan なし) と区別して unavailable ブロックを返す
        (codex Medium): None に潰すと planner が「見ていない既存 plan」を置き換えうる。
        P-4 gate が position と同様に direct_hold へ倒し、snapshot にも
        unavailable が残るため事後検証可能。
        """
        try:
            plans = self._orch.get_active_plans(pair)
            if not plans:
                return None
            plan = plans[0]
            return {
                "plan_id": plan.plan_id,
                "status": plan.status,
                "direction": plan.direction,
                "entry_summary": _entry_summary(plan.entry_conditions_json),
                "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
                "created_at": plan.created_at.isoformat() if plan.created_at else None,
            }
        except Exception:
            logger.warning("[ORCH] current_plan read failed for %s", pair, exc_info=True)
            return {"status": "unavailable"}

    @staticmethod
    def _empty_position() -> dict[str, Any]:
        return {"count": 0, "items": []}

    @staticmethod
    def _empty_news() -> dict[str, Any]:
        return {"sentiment_score": None, "confidence": None, "top_reasons": []}

    def _build_risk_state(self, pair: str) -> dict[str, Any]:
        """risk_state_provider 経由で実 gate 状態を取る。未注入なら楽観既定。

        provider 失敗時は安全側 (_SAFE_RISK_STATE) に倒す: gate 状態が読めないのに
        market_open=True の楽観既定に倒すと、休場/halt 中でも planning/watch が通る
        (Codex High)。production では bootstrap が必ず provider を注入する。
        """
        if self._risk_state_provider is None:
            return self._empty_risk_state()
        try:
            return self._risk_state_provider(pair)
        except Exception:
            logger.warning(
                "[ORCH] risk_state provider failed for %s — failing safe (no trigger)",
                pair, exc_info=True,
            )
            return dict(_SAFE_RISK_STATE)

    @staticmethod
    def _empty_risk_state() -> dict[str, Any]:
        return {"halt": "none", "bridge_health": "ok", "market_open": True, "cooldown": False}

    @staticmethod
    def _empty_trade_stats() -> dict[str, Any]:
        return {
            "window_hours": 24, "order_count": 0, "win_count": 0, "loss_count": 0,
            "open_position_count": 0, "net_exposure": 0.0, "last_order_at": None,
        }

    @staticmethod
    def _empty_move_maturity() -> dict[str, Any]:
        return {
            "extension_from_ma": None, "overbought_oversold": None,
            "dist_from_recent_swing": None,
        }


def make_news_provider(config: "AppConfig", store: "VectorStore") -> NewsProvider:
    """既存 RAG 集計 (aggregate_news_sentiment) を §7 news provider に適合させる。

    DecisionContextBuilder は news を `Callable[[str], dict]` として受ける (RAG/LLM へ
    直接依存しない)。本 factory が pair 文字列 → InstrumentConfig 解決 + 集計 +
    §7 shape ({sentiment_score, confidence, top_reasons}) への変換を担うアダプタ。

    NewsSentiment.key_themes を top_reasons に写す。config に無い pair は集計せず
    空 news に倒す (新規取得は一切しない: 保存済み RAG の集約のみ)。

    import はローカル: aggregate_news_sentiment が RAG/LLM スタックを引き込むため、
    context_builder の module-level import を軽量に保つ (trading.py と同じ方針)。
    """
    from src.analysis.news_aggregator import aggregate_news_sentiment

    by_symbol = {inst.symbol: inst for inst in config.enabled_instruments}

    def provider(pair: str) -> dict[str, Any]:
        pair_cfg = by_symbol.get(pair)
        if pair_cfg is None:
            return DecisionContextBuilder._empty_news()
        sentiment = aggregate_news_sentiment(pair_cfg, store, config)
        return {
            "sentiment_score": sentiment.sentiment_score,
            "confidence": sentiment.confidence,
            "top_reasons": list(sentiment.key_themes),
        }

    return provider


def _entry_summary(conditions: list | None) -> str:
    """entry_conditions_json の短縮表記 (先頭2条件)。

    条件 type ごとに参照キーが違う (codex Medium): price 系=value /
    spread_below=value_pips / technical_status_is=status。既知キーを順に引き、
    どれも無ければ type のみ。
    """
    if not conditions:
        return ""

    def _one(c: dict) -> str:
        for key in ("value", "value_pips", "status"):
            if c.get(key) is not None:
                return f"{c.get('type')} {c[key]}"
        return str(c.get("type"))

    parts = [_one(c) for c in conditions[:2]]
    if len(conditions) > 2:
        parts.append(f"(+{len(conditions) - 2} more)")
    return ", ".join(parts)
