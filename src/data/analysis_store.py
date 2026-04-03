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
        w_long = 0.0
        w_short = 0.0

        for snap in snapshots:
            hours_ago = (now - snap.analyzed_at).total_seconds() / 3600
            w = 1.0 / (1.0 + hours_ago)
            total_w += w
            w_bias += w * (snap.bias_score or 0.0)
            w_conf += w * (snap.confidence or 0.5)
            if snap.direction_bias == "long":
                w_long += w
            elif snap.direction_bias == "short":
                w_short += w

        agg_bias = w_bias / total_w

        # 方向一致率（時間加重）: 全スナップショットが同方向なら1.0、半々なら0.5
        consistency = max(w_long, w_short) / total_w if total_w > 0 else 0.5
        agg_conf = min(0.9, (w_conf / total_w) * consistency)

        direction = (
            "long"    if agg_bias > 0.1  else
            "short"   if agg_bias < -0.1 else
            "neutral"
        )

        # 方向カウント（ログ・reasoning用）
        dir_counts: dict[str, int] = {}
        for snap in snapshots:
            d = snap.direction_bias or "neutral"
            dir_counts[d] = dir_counts.get(d, 0) + 1

        # SL/TP は集約方向に一致する最新スナップショットを採用。
        # 一致するものがなければ最新スナップショットを使用する（安全ネットは signal_combiner 側に別途実装）。
        latest = snapshots[0]
        ref = next((s for s in snapshots if s.direction_bias == direction), latest)

        logger.info(
            f"[AGGREGATE] {symbol}: {len(snapshots)} snapshots | "
            f"bias={agg_bias:+.2f} conf={agg_conf:.2f} dir={direction} "
            f"consistency={consistency:.0%} (L={dir_counts.get('long', 0)} S={dir_counts.get('short', 0)} N={dir_counts.get('neutral', 0)})"
            + ("" if ref is latest else f" (SL/TP from direction-matched snapshot)")
        )
        return PriceAnalysis(
            pair=symbol,
            direction_bias=direction,
            bias_score=max(-1.0, min(1.0, agg_bias)),
            confidence=agg_conf,
            entry_zone=(ref.entry_zone_low or 0.0, ref.entry_zone_high or 0.0),
            stop_loss=ref.stop_loss or 0.0,
            take_profit=ref.take_profit or 0.0,
            risk_reward_ratio=ref.risk_reward_ratio or 2.0,
            reasoning_summary=(
                f"Aggregated {len(snapshots)} snapshots over {hours}h "
                f"(weighted bias={agg_bias:+.2f}, consistency={consistency:.0%}, "
                f"long={dir_counts.get('long', 0)} short={dir_counts.get('short', 0)} "
                f"neutral={dir_counts.get('neutral', 0)}, "
                f"latest: {latest.reasoning_summary or ''})"
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


# ── 予測サイクル用ストア ─────────────────────────────────────────────────────

class _ForecastRecord(_Base):
    """総合分析予測レコード（技術+ニュース合成シグナルを保存）。"""
    __tablename__ = "forecasts"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    pair                = Column(String,  nullable=False, index=True)
    forecast_ts         = Column(DateTime, nullable=False)
    current_price       = Column(Float,   nullable=False)
    predicted_direction = Column(String,  nullable=False)
    combined_score      = Column(Float,   nullable=False)
    confidence          = Column(Float,   nullable=False)
    signal_reason       = Column(String)
    stop_loss           = Column(Float)   # ATR proxy 算出用
    reviewed            = Column(Integer, default=0)
    # reviewed: 0=未検証 1=検証済(少なくとも1回) 3=skip(score不足)
    latest_review_ts    = Column(DateTime, nullable=True)   # 最終検証時刻
    latest_price_delta  = Column(Float,    nullable=True)   # 最終検証時の価格変化量
    macro_context_at_forecast = Column(String, nullable=True)  # 予測時の監視銘柄スナップショット


class ForecastStore:
    """総合分析予測の保存・取得・クリーンアップ。"""

    _PRUNE_OLDER_THAN_HOURS = 168  # 7日

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)

    def save_forecast(self, pair: str, signal, macro_context: str = "") -> None:
        """TradeSignal を予測レコードとして保存する。"""
        with Session(self._engine) as session:
            rec = _ForecastRecord(
                pair=pair,
                forecast_ts=datetime.now(),
                current_price=signal.entry_price,
                predicted_direction=signal.predicted_direction,
                combined_score=signal.combined_score,
                confidence=signal.confidence,
                signal_reason=signal.signal_reason,
                stop_loss=signal.stop_loss,
                reviewed=0,
                macro_context_at_forecast=macro_context or None,
            )
            session.add(rec)
            session.commit()
        logger.info(
            f"[FORECAST] Saved: {pair} {signal.predicted_direction} "
            f"score={signal.combined_score:+.3f} conf={signal.confidence:.2f}"
        )

    def save_forecast_skip(self, pair: str, signal) -> None:
        """スコア不足でスキップした予測をレコードとして保存する（reviewed=3）。"""
        with Session(self._engine) as session:
            rec = _ForecastRecord(
                pair=pair,
                forecast_ts=datetime.now(),
                current_price=signal.entry_price,
                predicted_direction=signal.predicted_direction,
                combined_score=signal.combined_score,
                confidence=signal.confidence,
                signal_reason=signal.signal_reason,
                stop_loss=signal.stop_loss,
                reviewed=3,
            )
            session.add(rec)
            session.commit()
        logger.info(
            f"[FORECAST] {pair}: skip — score={signal.combined_score:+.3f} (score_below_threshold)"
        )

    def get_recent_forecasts(self, pair: str, hours: int = 24) -> list[_ForecastRecord]:
        """直近N時間の予測レコードを古い順に返す（skip=3 は除外）。"""
        cutoff = datetime.now() - timedelta(hours=hours)
        with Session(self._engine) as session:
            stmt = (
                select(_ForecastRecord)
                .where(_ForecastRecord.pair == pair)
                .where(_ForecastRecord.reviewed != 3)
                .where(_ForecastRecord.forecast_ts >= cutoff)
                .order_by(_ForecastRecord.forecast_ts.asc())
            )
            results = session.execute(stmt).scalars().all()
            for r in results:
                session.expunge(r)
            return list(results)

    def get_recent_all(self, pair: str, hours: int = 24) -> list[_ForecastRecord]:
        """直近N時間の全レコード（skip含む）を古い順に返す。run forecast 表示用。"""
        cutoff = datetime.now() - timedelta(hours=hours)
        with Session(self._engine) as session:
            stmt = (
                select(_ForecastRecord)
                .where(_ForecastRecord.pair == pair)
                .where(_ForecastRecord.forecast_ts >= cutoff)
                .order_by(_ForecastRecord.forecast_ts.asc())
            )
            results = session.execute(stmt).scalars().all()
            for r in results:
                session.expunge(r)
            return list(results)

    def update_review(self, record_id: int, delta: float) -> None:
        """最新の検証結果（価格変化量）を上書き更新する。毎サイクル呼び出し。"""
        with Session(self._engine) as session:
            rec = session.get(_ForecastRecord, record_id)
            if rec:
                rec.reviewed = 1
                rec.latest_review_ts = datetime.now()
                rec.latest_price_delta = delta
                session.commit()

    def prune_old(self) -> None:
        """7日以上古いレコードを削除する。"""
        cutoff = datetime.now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
        with Session(self._engine) as session:
            old = session.execute(
                select(_ForecastRecord)
                .where(_ForecastRecord.forecast_ts < cutoff)
            ).scalars().all()
            for row in old:
                session.delete(row)
            if old:
                session.commit()
                logger.debug(f"Pruned {len(old)} old forecast records")


# ── HOLD判断レビュー用ストア ──────────────────────────────────────────────────

class _HoldDecisionRecord(_Base):
    """HOLD判断記録（次取引サイクルで検証し、RAGに蓄積）。"""
    __tablename__ = "hold_decisions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    pair                = Column(String,  nullable=False, index=True)
    decision_ts         = Column(DateTime, nullable=False)
    current_price       = Column(Float,   nullable=False)
    signal_score        = Column(Float,   nullable=False)  # combined_score
    predicted_direction = Column(String,  nullable=False)
    confidence          = Column(Float,   nullable=False)
    signal_reason       = Column(String)
    stop_loss           = Column(Float)   # ATR proxy 算出用
    reviewed            = Column(Integer, default=0)


class HoldDecisionStore:
    """HOLD判断の保存・取得・クリーンアップ。"""

    _PRUNE_OLDER_THAN_HOURS = 168  # 7日

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)

    def save_hold(self, pair: str, signal) -> None:
        """TradeSignal（HOLD判断）を記録する。"""
        with Session(self._engine) as session:
            rec = _HoldDecisionRecord(
                pair=pair,
                decision_ts=datetime.now(),
                current_price=signal.entry_price,
                signal_score=signal.combined_score,
                predicted_direction=signal.predicted_direction or "neutral",
                confidence=signal.confidence,
                signal_reason=signal.signal_reason,
                stop_loss=signal.stop_loss,
                reviewed=0,
            )
            session.add(rec)
            session.commit()
        logger.info(
            f"[HOLD] Recorded: {pair} {signal.predicted_direction} "
            f"score={signal.combined_score:+.3f} conf={signal.confidence:.2f}"
        )

    def get_unreviewed(self) -> list[_HoldDecisionRecord]:
        """未検証のHOLD記録を古い順で返す。"""
        with Session(self._engine) as session:
            stmt = (
                select(_HoldDecisionRecord)
                .where(_HoldDecisionRecord.reviewed == 0)
                .order_by(_HoldDecisionRecord.decision_ts.asc())
            )
            results = list(session.execute(stmt).scalars().all())
            for r in results:
                session.expunge(r)
            return results

    def mark_reviewed(self, record_id: int) -> None:
        """HOLD記録を検証済みにマークする。"""
        with Session(self._engine) as session:
            rec = session.get(_HoldDecisionRecord, record_id)
            if rec:
                rec.reviewed = 1
                session.commit()

    def prune_old(self) -> None:
        """7日以上古いレコードを削除する。"""
        cutoff = datetime.now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
        with Session(self._engine) as session:
            old = session.execute(
                select(_HoldDecisionRecord)
                .where(_HoldDecisionRecord.decision_ts < cutoff)
            ).scalars().all()
            for row in old:
                session.delete(row)
            if old:
                session.commit()
                logger.debug(f"Pruned {len(old)} old hold decision records")
