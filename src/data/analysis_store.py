"""テクニカル分析スナップショットの蓄積・時間加重集約ストア。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, Float, Integer, String, select, text
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.analysis.price_analyzer import PriceAnalysis
    from src.analysis.technical_snapshot_data import TechnicalSnapshotData

logger = logging.getLogger(__name__)


class _TechnicalSnapshot(_Base):
    """15分ごとのテクニカル分析スナップショット (決定的計算のみ、LLM 無し)。"""
    __tablename__ = "technical_snapshots"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String,  nullable=False, index=True)
    analyzed_at     = Column(DateTime, nullable=False)
    bias_score      = Column(Float)
    confidence      = Column(Float)
    direction_bias  = Column(String)
    collect_status  = Column(String, nullable=False)
    reason          = Column(String)   # sentinel 理由 (ok 行は NULL)
    mtf_alignment   = Column(Float)     # 単一 TF fallback 時 NULL
    tf_scores_json  = Column(String)    # {"long": {"score":..,"direction":..}, ...}
    components_json = Column(String)    # {"sma":.., "rsi":.., "adx_factor":..}
    patterns_json   = Column(String)    # ["engulfing_bullish", ...]


class AnalysisStore:
    """15分ごとのテクニカル分析スナップショットを蓄積・集約するストア。"""

    _PRUNE_OLDER_THAN_HOURS = 48  # これ以上古いスナップショットは自動削除

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)
        self._migrate()

    # 新スキーマの必須列 (欠けていれば再作成が必要)
    _REQUIRED_COLS = frozenset({
        "reason", "mtf_alignment", "tf_scores_json",
        "components_json", "patterns_json",
    })
    # 旧スキーマの痕跡列 (残っていれば再作成が必要)
    _LEGACY_COLS = frozenset({
        "stop_loss", "take_profit", "entry_zone_low", "entry_zone_high",
        "risk_reward_ratio", "reasoning_summary", "market_regime",
        "confidence_modifier",
    })

    def _migrate(self) -> None:
        """0 ベース再設計 (spec §2.B)。shape guard で一回性・冪等性を担保する。

        旧列が残っている / 新列が欠けている場合のみ DROP→CREATE する
        (データ移行なし)。新形状が揃っていれば no-op (ストア生成毎に走っても安全)。
        """
        with self._engine.connect() as conn:
            existing = {
                row[1]
                for row in conn.execute(
                    text("PRAGMA table_info(technical_snapshots)")
                ).fetchall()
            }
            if existing:  # テーブルが既存
                # __init__ は _get_engine を先に呼ぶため実運用ではここで常に存在する。
                # 空 set 分岐は _migrate を直接呼んだ場合の防御 (テーブル未作成)。
                needs_rebuild = bool(existing & self._LEGACY_COLS) or not (
                    self._REQUIRED_COLS <= existing
                )
                if needs_rebuild:
                    conn.execute(text("DROP TABLE technical_snapshots"))
                    conn.commit()
                    logger.info("[MIGRATE] technical_snapshots dropped for 0-based rebuild")
        # 新形状で作成 (存在すれば no-op)
        _Base.metadata.create_all(self._engine, tables=[_TechnicalSnapshot.__table__])

    def add_snapshot(self, data: "TechnicalSnapshotData") -> None:
        """決定的 technical snapshot を ok status で保存する (spec §2.B)。

        保存後 _prune_old(symbol) を呼び 48h 超の古い行を消す。
        """
        with Session(self._engine) as session:
            snap = _TechnicalSnapshot(
                symbol=data.pair,
                analyzed_at=data.analyzed_at,
                bias_score=data.bias_score,
                confidence=data.confidence,
                direction_bias=data.direction_bias,
                collect_status="ok",
                reason=None,
                mtf_alignment=data.mtf_alignment,
                tf_scores_json=json.dumps(data.tf_scores, ensure_ascii=False),
                components_json=json.dumps(data.components, ensure_ascii=False),
                patterns_json=json.dumps(data.patterns, ensure_ascii=False),
            )
            session.add(snap)
            session.commit()
        logger.debug(f"Stored ok snapshot for {data.pair} (bias={data.bias_score:+.2f})")
        self._prune_old(data.pair)

    _SENTINEL_ALLOWED = ("stale_price", "failed")
    _SENTINEL_REASON_MAX_LEN = 512

    def add_sentinel(
        self,
        symbol: str,
        status: str,
        reason: str,
        analyzed_at: "datetime | None" = None,
    ) -> None:
        """収集失敗を sentinel 行として保存。

        Args:
            status: 'stale_price' | 'failed' のみ。それ以外は ValueError。
            reason: 失敗理由。512 文字を超える場合は truncate して
                    ' ... [truncated]' を付与。
            analyzed_at: 省略時 db_now()。テスト用に注入可能。

        保存後 _prune_old(symbol) を呼び 48h 超の sentinel 行も消す
        (失敗連続で sentinel が DB を膨張させないため)。
        """
        if status not in self._SENTINEL_ALLOWED:
            raise ValueError(
                f"sentinel status must be one of {self._SENTINEL_ALLOWED}, got {status!r}"
            )
        if len(reason) > self._SENTINEL_REASON_MAX_LEN:
            reason = reason[: self._SENTINEL_REASON_MAX_LEN] + " ... [truncated]"

        if analyzed_at is None:
            analyzed_at = db_now()

        with Session(self._engine) as session:
            snap = _TechnicalSnapshot(
                symbol=symbol,
                analyzed_at=analyzed_at,
                bias_score=0.0,
                confidence=0.0,
                direction_bias="neutral",
                collect_status=status,
                reason=reason,
                mtf_alignment=None,
                tf_scores_json="{}",
                components_json="{}",
                patterns_json="[]",
            )
            session.add(snap)
            session.commit()
        logger.debug(f"Stored {status} sentinel for {symbol}: {reason[:80]}")
        self._prune_old(symbol)

    def get_recent_ok_snapshots(
        self, symbol: str, hours: int = 8,
    ) -> list[_TechnicalSnapshot]:
        """ok status のみ、lookback 内を新しい順で返す。

        取引判定 (aggregate 内部) / LLM プロンプト previous_analysis /
        RAG context / econ 分析 / health 確認で使う。sentinel 行は除外する。
        """
        since = db_now() - timedelta(hours=hours)
        with Session(self._engine) as session:
            stmt = (
                select(_TechnicalSnapshot)
                .where(_TechnicalSnapshot.symbol == symbol)
                .where(_TechnicalSnapshot.analyzed_at >= since)
                .where(_TechnicalSnapshot.collect_status == "ok")
                .order_by(_TechnicalSnapshot.analyzed_at.desc())
            )
            return list(session.execute(stmt).scalars().all())

    def get_latest_collect_row(self, symbol: str) -> _TechnicalSnapshot | None:
        """全 status の最新 1 行 (lookback 非依存)。

        run tech / /tech 表示の「Last collect / Status / Reason」用。
        取引判定・LLM 入力からは絶対呼ばない (lookback 非依存ゆえ古いデータ
        汚染リスクあり)。

        _prune_old(48h) で 48h より古い行は INSERT 時に消えるが、休場中で
        INSERT が走らない期間は古い行が保持される (e.g., 金曜の最終行が
        日曜まで残る)。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_TechnicalSnapshot)
                .where(_TechnicalSnapshot.symbol == symbol)
                .order_by(_TechnicalSnapshot.analyzed_at.desc())
                .limit(1)
            )
            return session.execute(stmt).scalars().first()

    def get_latest_ok_row(self, symbol: str) -> _TechnicalSnapshot | None:
        """ok status の最新 1 行 (lookback 非依存)。

        run tech / /tech 表示の「Last ok / Bias / Conf / Dir」用。
        取引判定からは絶対呼ばない (lookback 非依存)。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_TechnicalSnapshot)
                .where(_TechnicalSnapshot.symbol == symbol)
                .where(_TechnicalSnapshot.collect_status == "ok")
                .order_by(_TechnicalSnapshot.analyzed_at.desc())
                .limit(1)
            )
            return session.execute(stmt).scalars().first()

    _MAX_SNAPSHOTS = 16  # 集約対象の最大件数（直近N件に限定）
    _DECAY_HALF_LIFE_H = 2.0  # 指数減衰の半減期（時間）

    def aggregate(self, symbol: str, hours: int = 8) -> "PriceAnalysis | None":  # type: ignore[name-defined]
        """
        直近スナップショットを指数減衰加重で集約し PriceAnalysis を返す。
        データがなければ None を返す。

        重み: exp(-0.693 * hours_ago / half_life) — 半減期で重みが半分に。
        直近 _MAX_SNAPSHOTS 件に限定し、トレンド転換時の古いノイズを排除する。
        SL/TP/エントリーゾーン/market_regime/confidence_modifier は
        LLM廃止に伴い削除済みの列のため既定値 (0.0 等) を返す。
        """
        import math
        from src.analysis.price_analyzer import PriceAnalysis  # local import

        snapshots = self.get_recent_ok_snapshots(symbol, hours)
        if not snapshots:
            return None

        snapshots = snapshots[:self._MAX_SNAPSHOTS]

        now = db_now()
        decay_rate = 0.693 / self._DECAY_HALF_LIFE_H  # ln(2) / half_life
        total_w = 0.0
        w_bias = 0.0
        w_conf = 0.0
        w_long = 0.0
        w_short = 0.0

        for snap in snapshots:
            hours_ago = (now - snap.analyzed_at).total_seconds() / 3600
            w = math.exp(-decay_rate * hours_ago)
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
            "long"    if agg_bias > 0.05  else
            "short"   if agg_bias < -0.05 else
            "neutral"
        )

        # 方向カウント（ログ・reasoning用）
        dir_counts: dict[str, int] = {}
        for snap in snapshots:
            d = snap.direction_bias or "neutral"
            dir_counts[d] = dir_counts.get(d, 0) + 1

        logger.info(
            f"[AGGREGATE] {symbol}: {len(snapshots)} snapshots | "
            f"bias={agg_bias:+.2f} conf={agg_conf:.2f} dir={direction} "
            f"consistency={consistency:.0%} (L={dir_counts.get('long', 0)} "
            f"S={dir_counts.get('short', 0)} N={dir_counts.get('neutral', 0)})"
        )
        return PriceAnalysis(
            pair=symbol,
            direction_bias=direction,
            bias_score=max(-1.0, min(1.0, agg_bias)),
            confidence=agg_conf,
            entry_zone=(0.0, 0.0),
            stop_loss=0.0,
            take_profit=0.0,
            risk_reward_ratio=2.0,
            reasoning_summary=(
                f"Aggregated {len(snapshots)} snapshots over {hours}h "
                f"(weighted bias={agg_bias:+.2f}, consistency={consistency:.0%}, "
                f"long={dir_counts.get('long', 0)} short={dir_counts.get('short', 0)} "
                f"neutral={dir_counts.get('neutral', 0)})"
            ),
            analyzed_at=db_now(),
            market_regime="unknown",
            confidence_modifier=0.0,
        )

    def _prune_old(self, symbol: str) -> None:
        """古いスナップショットを削除する。"""
        cutoff = db_now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
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
                forecast_ts=db_now(),
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
                forecast_ts=db_now(),
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
        cutoff = db_now() - timedelta(hours=hours)
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
        cutoff = db_now() - timedelta(hours=hours)
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
                rec.latest_review_ts = db_now()
                rec.latest_price_delta = delta
                session.commit()

    def prune_old(self) -> None:
        """7日以上古いレコードを削除する。"""
        cutoff = db_now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
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
                decision_ts=db_now(),
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
        cutoff = db_now() - timedelta(hours=self._PRUNE_OLDER_THAN_HOURS)
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
