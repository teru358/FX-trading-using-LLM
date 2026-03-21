"""テクニカル分析スナップショットの蓄積・時間加重集約ストア。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Float, Integer, String, select
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine

logger = logging.getLogger(__name__)


class _TechnicalSnapshot(_Base):
    """15分ごとのテクニカル分析スナップショット。"""
    __tablename__ = "technical_snapshots"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String,  nullable=False, index=True)
    analyzed_at     = Column(DateTime, nullable=False)
    bias_score      = Column(Float)
    confidence      = Column(Float)
    direction_bias  = Column(String)
    stop_loss       = Column(Float)
    take_profit     = Column(Float)
    entry_zone_low  = Column(Float)
    entry_zone_high = Column(Float)
    risk_reward_ratio = Column(Float)
    reasoning_summary = Column(String)


class AnalysisStore:
    """15分ごとのテクニカル分析スナップショットを蓄積・集約するストア。"""

    _PRUNE_OLDER_THAN_HOURS = 48  # これ以上古いスナップショットは自動削除

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)

    def upsert_snapshot(self, analysis: "PriceAnalysis") -> None:  # type: ignore[name-defined]
        """PriceAnalysis をスナップショットとして保存する。"""
        with Session(self._engine) as session:
            snap = _TechnicalSnapshot(
                symbol=analysis.pair,
                analyzed_at=analysis.analyzed_at,
                bias_score=analysis.bias_score,
                confidence=analysis.confidence,
                direction_bias=analysis.direction_bias,
                stop_loss=analysis.stop_loss,
                take_profit=analysis.take_profit,
                entry_zone_low=analysis.entry_zone[0],
                entry_zone_high=analysis.entry_zone[1],
                risk_reward_ratio=analysis.risk_reward_ratio,
                reasoning_summary=analysis.reasoning_summary,
            )
            session.add(snap)
            session.commit()
        logger.debug(f"Stored technical snapshot for {analysis.pair} (bias={analysis.bias_score:+.2f})")
        self._prune_old(analysis.pair)

    def get_recent_snapshots(
        self, symbol: str, hours: int = 8
    ) -> list[_TechnicalSnapshot]:
        """直近 hours 時間以内のスナップショットを新しい順で返す。"""
        since = datetime.now() - timedelta(hours=hours)
        with Session(self._engine) as session:
            stmt = (
                select(_TechnicalSnapshot)
                .where(_TechnicalSnapshot.symbol == symbol)
                .where(_TechnicalSnapshot.analyzed_at >= since)
                .order_by(_TechnicalSnapshot.analyzed_at.desc())
            )
            return list(session.execute(stmt).scalars().all())

    def aggregate(self, symbol: str, hours: int = 8) -> "PriceAnalysis | None":  # type: ignore[name-defined]
        """
        直近スナップショットを時間加重平均で集約し PriceAnalysis を返す。
        データがなければ None を返す。

        重みは 1/(1+経過時間[h]) — 新しいほど重く評価。
        SL/TP/エントリーゾーンは最新スナップショットの値を使用。
        """
        from src.analysis.price_analyzer import PriceAnalysis  # local import

        snapshots = self.get_recent_snapshots(symbol, hours)
        if not snapshots:
            return None

        now = datetime.now()
        total_w = 0.0
        w_bias = 0.0
        w_conf = 0.0

        for snap in snapshots:
            hours_ago = (now - snap.analyzed_at).total_seconds() / 3600
            w = 1.0 / (1.0 + hours_ago)
            total_w += w
            w_bias += w * (snap.bias_score or 0.0)
            w_conf += w * (snap.confidence or 0.5)

        agg_bias = w_bias / total_w
        agg_conf = min(0.9, w_conf / total_w)

        # 直近スナップショットの価格水準を採用
        latest = snapshots[0]
        direction = (
            "long"    if agg_bias > 0.1  else
            "short"   if agg_bias < -0.1 else
            "neutral"
        )

        logger.info(
            f"[AGGREGATE] {symbol}: {len(snapshots)} snapshots | "
            f"bias={agg_bias:+.2f} conf={agg_conf:.2f} dir={direction}"
        )
        return PriceAnalysis(
            pair=symbol,
            direction_bias=direction,
            bias_score=max(-1.0, min(1.0, agg_bias)),
            confidence=agg_conf,
            entry_zone=(latest.entry_zone_low or 0.0, latest.entry_zone_high or 0.0),
            stop_loss=latest.stop_loss or 0.0,
            take_profit=latest.take_profit or 0.0,
            risk_reward_ratio=latest.risk_reward_ratio or 2.0,
            reasoning_summary=(
                f"Aggregated {len(snapshots)} snapshots over {hours}h "
                f"(weighted bias={agg_bias:+.2f}, latest: {latest.reasoning_summary or ''})"
            ),
            analyzed_at=datetime.now(),
        )

    def _prune_old(self, symbol: str) -> None:
        """古いスナップショットを削除する。"""
        cutoff = datetime.now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
        with Session(self._engine) as session:
            old = session.execute(
                select(_TechnicalSnapshot)
                .where(_TechnicalSnapshot.symbol == symbol)
                .where(_TechnicalSnapshot.analyzed_at < cutoff)
            ).scalars().all()
            for row in old:
                session.delete(row)
            if old:
                session.commit()
                logger.debug(f"Pruned {len(old)} old snapshots for {symbol}")
