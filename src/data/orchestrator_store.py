"""orchestrator agent loop の判断 trace / plan テーブル群 (spec §8)。

既存 price_store.py の _Base / _get_engine を再利用し、同一 SQLite DB に
追加テーブルを作る。JSON カラムは SQLAlchemy の JSON 型を使う (SQLite では
TEXT に格納される)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON, Column, DateTime, Float, Integer, String, UniqueConstraint, select,
)
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


class _DecisionSnapshot(_Base):
    """decision 開始時に materialize する入力スナップショット (spec §8.7)。"""
    __tablename__ = "decision_snapshots"

    snapshot_id   = Column(Integer, primary_key=True, autoincrement=True)
    pair          = Column(String, nullable=False, index=True)
    as_of_time    = Column(DateTime, nullable=False)
    quote_json    = Column(JSON)
    technical_ref = Column(JSON)
    news_ref      = Column(JSON)
    created_at    = Column(DateTime, nullable=False)


class _AgentRun(_Base):
    """agent 実行単位のメタデータ (spec §8.1)。"""
    __tablename__ = "agent_runs"

    run_id              = Column(Integer, primary_key=True, autoincrement=True)
    agent_name          = Column(String, nullable=False, index=True)
    agent_version       = Column(String)
    pair                = Column(String, index=True)
    trigger_type        = Column(String)
    status              = Column(String, nullable=False)  # ok | failed | skipped
    started_at          = Column(DateTime)
    finished_at         = Column(DateTime)
    model_name          = Column(String)
    input_context_hash  = Column(String)
    snapshot_id         = Column(Integer, index=True)  # → decision_snapshots
    trade_horizon       = Column(String)               # day | swing
    error_type          = Column(String)
    error_message       = Column(String)


class OrchestratorStore:
    """spec §8 のテーブル群を 1 つの DB で管理するストア。"""

    def __init__(self, db_path: Path) -> None:
        self._engine = _get_engine(db_path)

    # ── decision_snapshots (§8.7) ──────────────────────────────

    def create_snapshot(
        self,
        pair: str,
        as_of_time: datetime,
        quote_json: dict[str, Any] | None = None,
        technical_ref: dict[str, Any] | None = None,
        news_ref: dict[str, Any] | None = None,
    ) -> int:
        """decision_snapshot を materialize し snapshot_id を返す。"""
        with Session(self._engine) as session:
            snap = _DecisionSnapshot(
                pair=pair,
                as_of_time=as_of_time,
                quote_json=quote_json,
                technical_ref=technical_ref,
                news_ref=news_ref,
                created_at=db_now(),
            )
            session.add(snap)
            session.commit()
            return snap.snapshot_id

    def get_snapshot(self, snapshot_id: int) -> _DecisionSnapshot | None:
        with Session(self._engine) as session:
            snap = session.get(_DecisionSnapshot, snapshot_id)
            if snap is not None:
                session.expunge(snap)
            return snap

    # ── agent_runs (§8.1) ──────────────────────────────────────

    def start_run(
        self,
        agent_name: str,
        *,
        pair: str | None = None,
        trigger_type: str | None = None,
        snapshot_id: int | None = None,
        model_name: str | None = None,
        trade_horizon: str | None = None,
        agent_version: str | None = None,
        input_context_hash: str | None = None,
    ) -> int:
        """agent run を status=ok・started_at=now で開始し run_id を返す。"""
        with Session(self._engine) as session:
            run = _AgentRun(
                agent_name=agent_name,
                agent_version=agent_version,
                pair=pair,
                trigger_type=trigger_type,
                status="ok",
                started_at=db_now(),
                model_name=model_name,
                input_context_hash=input_context_hash,
                snapshot_id=snapshot_id,
                trade_horizon=trade_horizon,
            )
            session.add(run)
            session.commit()
            return run.run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "ok",
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """run を終了マークする (status / error / finished_at を更新)。"""
        with Session(self._engine) as session:
            run = session.get(_AgentRun, run_id)
            if run is None:
                logger.warning(f"finish_run: run_id {run_id} not found")
                return
            run.status = status
            run.finished_at = db_now()
            run.error_type = error_type
            run.error_message = error_message
            session.commit()

    def get_run(self, run_id: int) -> _AgentRun | None:
        with Session(self._engine) as session:
            run = session.get(_AgentRun, run_id)
            if run is not None:
                session.expunge(run)
            return run
