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

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore

logger = logging.getLogger(__name__)


@dataclass
class QuoteSnapshot:
    """watch 側から渡される瞬間 quote (planning は snapshot 経由で読む)。"""
    bid: float
    ask: float
    mid: float
    spread: float
    source: str
    observed_at: datetime


class ContextBuilder:
    """decision_snapshot を作り §7 標準 context を組む層 (決定的)。"""

    def __init__(
        self,
        orch_store: OrchestratorStore,
        analysis_store: AnalysisStore,
        config: OrchestratorConfig,
    ) -> None:
        self._orch = orch_store
        self._analysis = analysis_store
        self._config = config

    def build(self, *, pair: str, now: datetime, quote: QuoteSnapshot) -> dict:
        """decision_snapshot を materialize し §7 標準 context dict を返す。"""
        technical = self._build_technical(pair, now)
        quote_dict = {
            "bid": quote.bid, "ask": quote.ask, "mid": quote.mid,
            "spread": quote.spread, "source": quote.source,
            "observed_at": quote.observed_at.isoformat(),
        }

        snapshot_id = self._orch.create_snapshot(
            pair=pair,
            as_of_time=now,
            quote_json=quote_dict,
            technical_ref=technical.get("_ref"),
            news_ref=None,
        )

        return {
            "snapshot_id": snapshot_id,
            "pair": pair,
            "now": now.isoformat(),
            "quote": quote_dict,
            "position": self._empty_position(),
            "technical": {k: v for k, v in technical.items() if k != "_ref"},
            "news": self._empty_news(),
            "risk_state": self._empty_risk_state(),
            "data_health": {"issues": []},
            "recent_decisions": {"items": []},
            "recent_orders": {"items": []},
            "recent_exits": {"items": []},
            "recent_trade_stats": self._empty_trade_stats(),
            "move_maturity": self._empty_move_maturity(),
            "similar_cases": {"items": []},
            "policy": {
                "trade_horizon": self._config.policy.trade_horizon,
                "advice_memo": self._config.policy.advice_memo or None,
            },
        }

    # technical snapshot をこの分数より古ければ stale 扱いにする (decision に
    # 古いデータを使わせない)。lookback 窓もこの値から算出する。
    _TECHNICAL_MAX_STALE_MINUTES = 30

    def _build_technical(self, pair: str, now: datetime) -> dict:
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
        max_stale = timedelta(minutes=self._TECHNICAL_MAX_STALE_MINUTES)
        # 窓は max_stale を十分内包する広さにする (24h)。これにより max_stale 超過の
        # 行も取得され、age>max_stale で stale に分類できる (missing と区別可能)。
        lookback_hours = 24
        rows = self._analysis.get_recent_ok_snapshots(pair, hours=lookback_hours)
        if not rows:
            return {
                "status": "missing", "bias_score": None, "confidence": None,
                "direction": None, "last_ok_at": None, "_ref": None,
            }

        row = rows[0]  # get_recent_ok_snapshots は新しい順
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

    @staticmethod
    def _empty_position() -> dict:
        return {"side": None, "entry": None, "size": None, "pnl": None, "mfe_r": None}

    @staticmethod
    def _empty_news() -> dict:
        return {"sentiment_score": None, "confidence": None, "top_reasons": []}

    @staticmethod
    def _empty_risk_state() -> dict:
        return {"halt": "none", "bridge_health": "ok", "market_open": True, "cooldown": False}

    @staticmethod
    def _empty_trade_stats() -> dict:
        return {
            "window_hours": 24, "order_count": 0, "win_count": 0, "loss_count": 0,
            "open_position_count": 0, "net_exposure": 0.0, "last_order_at": None,
        }

    @staticmethod
    def _empty_move_maturity() -> dict:
        return {
            "extension_from_ma": None, "overbought_oversold": None,
            "dist_from_recent_swing": None,
        }
