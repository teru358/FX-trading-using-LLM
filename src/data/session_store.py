# src/data/session_store.py
"""取引セッションのライフサイクル管理ストア。"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, String, Text
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine

logger = logging.getLogger(__name__)


class _TradingSession(_Base):
    """取引セッション: 発注→クローズの1サイクルを追跡する。"""
    __tablename__ = "trading_sessions"

    session_id        = Column(String, primary_key=True)
    pair              = Column(String, nullable=False, index=True)
    direction         = Column(String, nullable=False)
    entry_price       = Column(Float, nullable=False)
    stop_loss         = Column(Float)
    take_profit       = Column(Float)
    position_size     = Column(Float)
    signal_score      = Column(Float)
    signal_confidence = Column(Float)
    macro_context     = Column(Text)
    analysis_summary  = Column(Text)
    opened_at         = Column(DateTime, nullable=False)
    closed_at         = Column(DateTime)
    close_price       = Column(Float)
    close_reason      = Column(String)
    realized_pnl      = Column(Float)
    outcome           = Column(String)
    reflection_text   = Column(Text)
    created_at        = Column(DateTime, nullable=False, default=datetime.now)
    # ATR SL/TP比較データ
    atr_value       = Column(Float)
    sl_atr_mult     = Column(Float)
    tp_atr_mult     = Column(Float)
    computed_sl     = Column(Float)
    computed_tp     = Column(Float)
    llm_sl          = Column(Float)
    llm_tp          = Column(Float)
    key_support     = Column(Float)
    key_resistance  = Column(Float)


class SessionStore:
    """trading_sessions テーブルの CRUD。"""

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)
        self._migrate()

    def _migrate(self) -> None:
        new_columns = [
            "atr_value", "sl_atr_mult", "tp_atr_mult",
            "computed_sl", "computed_tp", "llm_sl", "llm_tp",
            "key_support", "key_resistance",
        ]
        from sqlalchemy import text, inspect
        insp = inspect(self._engine)
        if "trading_sessions" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("trading_sessions")}
        with self._engine.begin() as conn:
            for col in new_columns:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE trading_sessions ADD COLUMN {col} REAL"))
                    logger.info(f"[SESSION] Migration: added column {col}")

    def create_session(
        self,
        session_id: str,
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        signal_score: float,
        signal_confidence: float,
        macro_context: str,
        analysis_summary: str,
        opened_at: datetime,
        # ATR SL/TP比較データ（オプショナル）
        atr_value: float | None = None,
        sl_atr_mult: float | None = None,
        tp_atr_mult: float | None = None,
        computed_sl: float | None = None,
        computed_tp: float | None = None,
        llm_sl: float | None = None,
        llm_tp: float | None = None,
        key_support: float | None = None,
        key_resistance: float | None = None,
    ) -> None:
        with Session(self._engine) as session:
            rec = _TradingSession(
                session_id=session_id,
                pair=pair,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=position_size,
                signal_score=signal_score,
                signal_confidence=signal_confidence,
                macro_context=macro_context,
                analysis_summary=analysis_summary,
                opened_at=opened_at,
                created_at=datetime.now(),
                atr_value=atr_value,
                sl_atr_mult=sl_atr_mult,
                tp_atr_mult=tp_atr_mult,
                computed_sl=computed_sl,
                computed_tp=computed_tp,
                llm_sl=llm_sl,
                llm_tp=llm_tp,
                key_support=key_support,
                key_resistance=key_resistance,
            )
            session.add(rec)
            session.commit()
        logger.info(f"[SESSION] Created: {session_id} {pair} {direction}")

    def get_session(self, session_id: str) -> _TradingSession | None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec:
                session.expunge(rec)
            return rec

    def close_session(
        self,
        session_id: str,
        closed_at: datetime,
        close_price: float,
        close_reason: str,
        realized_pnl: float,
        reflection_text: str = "",
    ) -> None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec is None:
                logger.warning(f"[SESSION] close_session: {session_id} not found")
                return
            rec.closed_at = closed_at
            rec.close_price = close_price
            rec.close_reason = close_reason
            rec.realized_pnl = realized_pnl
            rec.outcome = "win" if realized_pnl > 0 else "loss"
            rec.reflection_text = reflection_text or None
            session.commit()
        logger.info(
            f"[SESSION] Closed: {session_id} reason={close_reason} "
            f"pnl={realized_pnl:+.2f} outcome={'win' if realized_pnl > 0 else 'loss'}"
        )

    def update_reflection(self, session_id: str, reflection_text: str) -> None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec:
                rec.reflection_text = reflection_text
                session.commit()
