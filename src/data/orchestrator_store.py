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
from sqlalchemy.exc import IntegrityError
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


# spec §8.9 が許可する plan status の集合 (watch loop は active 以外を執行しない)
PLAN_STATUSES = (
    "active", "triggered", "expired", "invalidated",
    "superseded", "suspended", "requires_replan",
)


class _TradePlan(_Base):
    """PlannerAgent が立てる条件付き発注意図 (spec §8.9)。"""
    __tablename__ = "trade_plans"

    plan_id               = Column(Integer, primary_key=True, autoincrement=True)
    pair                  = Column(String, nullable=False, index=True)
    snapshot_id           = Column(Integer)
    horizon               = Column(String)   # day | swing
    direction             = Column(String)   # long | short
    entry_conditions_json = Column(JSON)
    action_json           = Column(JSON)
    invalidation_json     = Column(JSON)
    expires_at            = Column(DateTime)
    status                = Column(String, nullable=False, index=True)
    created_by_run_id     = Column(Integer)
    created_at            = Column(DateTime, nullable=False)
    updated_at            = Column(DateTime, nullable=False)


# spec §8.8 の order_intents status / recovery_status の集合
ORDER_INTENT_STATUSES = (
    "pending", "submitted", "filled", "rejected",
    "failed", "needs_reconcile", "abandoned",
)


class _OrderIntent(_Base):
    """二重発注を durable に防ぐテーブル (spec §8.8)。plan_id UNIQUE。"""
    __tablename__ = "order_intents"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    plan_id            = Column(Integer, nullable=False)
    trigger_id         = Column(String)
    decision_id        = Column(Integer)
    pair               = Column(String, nullable=False)
    intended_action    = Column(String, nullable=False)  # buy | sell
    status             = Column(String, nullable=False)
    owner_run_id       = Column(Integer)
    lease_until        = Column(DateTime)
    submitted_at       = Column(DateTime)   # null = broker 送信前 (送信前/送信後の分岐点)
    recovery_status    = Column(String)     # null | needs_reconcile | retryable | abandoned
    order_id           = Column(String)
    broker_result_json = Column(JSON)
    created_at         = Column(DateTime, nullable=False)
    updated_at         = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_order_intents_plan_id"),
    )


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

    # ── trade_plans (§8.9) ─────────────────────────────────────

    def create_trade_plan(
        self,
        *,
        pair: str,
        snapshot_id: int,
        horizon: str,
        direction: str,
        entry_conditions_json: list,
        action_json: dict,
        invalidation_json: list,
        expires_at: datetime,
        created_by_run_id: int,
    ) -> int:
        """active な trade_plan を作成し plan_id を返す。"""
        now = db_now()
        with Session(self._engine) as session:
            plan = _TradePlan(
                pair=pair,
                snapshot_id=snapshot_id,
                horizon=horizon,
                direction=direction,
                entry_conditions_json=entry_conditions_json,
                action_json=action_json,
                invalidation_json=invalidation_json,
                expires_at=expires_at,
                status="active",
                created_by_run_id=created_by_run_id,
                created_at=now,
                updated_at=now,
            )
            session.add(plan)
            session.commit()
            return plan.plan_id

    def get_trade_plan(self, plan_id: int) -> _TradePlan | None:
        with Session(self._engine) as session:
            plan = session.get(_TradePlan, plan_id)
            if plan is not None:
                session.expunge(plan)
            return plan

    def update_plan_status(self, plan_id: int, status: str) -> None:
        if status not in PLAN_STATUSES:
            raise ValueError(f"status must be one of {PLAN_STATUSES}, got {status!r}")
        with Session(self._engine) as session:
            plan = session.get(_TradePlan, plan_id)
            if plan is None:
                logger.warning(f"update_plan_status: plan_id {plan_id} not found")
                return
            plan.status = status
            plan.updated_at = db_now()
            session.commit()

    def get_active_plans(self, pair: str | None = None) -> list[_TradePlan]:
        """status=active の plan を返す (pair 指定で絞り込み)。"""
        with Session(self._engine) as session:
            stmt = select(_TradePlan).where(_TradePlan.status == "active")
            if pair is not None:
                stmt = stmt.where(_TradePlan.pair == pair)
            plans = list(session.execute(stmt).scalars().all())
            for p in plans:
                session.expunge(p)
            return plans

    # ── order_intents (§8.8) ───────────────────────────────────

    def try_insert_order_intent(
        self,
        *,
        plan_id: int,
        pair: str,
        intended_action: str,
        owner_run_id: int,
        lease_until: datetime,
        decision_id: int | None = None,
        trigger_id: str | None = None,
    ) -> bool:
        """発注前の pending intent を INSERT する。

        plan_id UNIQUE 違反 (= 既発注) なら False を返し、呼び出し側は
        発注を中止する。成功すれば True。
        """
        now = db_now()
        with Session(self._engine) as session:
            intent = _OrderIntent(
                plan_id=plan_id,
                trigger_id=trigger_id,
                decision_id=decision_id,
                pair=pair,
                intended_action=intended_action,
                status="pending",
                owner_run_id=owner_run_id,
                lease_until=lease_until,
                submitted_at=None,
                recovery_status=None,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            try:
                session.commit()
                return True
            except IntegrityError as exc:
                session.rollback()
                # plan_id UNIQUE 違反のみ「既発注」として False を返す。
                # それ以外の IntegrityError (NOT NULL 等のバグ) は握り潰さず再送出し、
                # 「既発注」と誤判定して取引を黙って飛ばす silent failure を防ぐ。
                orig = str(getattr(exc, "orig", exc))
                if (
                    "uq_order_intents_plan_id" in orig
                    or "order_intents.plan_id" in orig
                ):
                    logger.info(
                        f"order_intent INSERT rejected: plan_id {plan_id} already exists"
                    )
                    return False
                raise

    def get_order_intent(self, plan_id: int) -> _OrderIntent | None:
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.plan_id == plan_id)
            intent = session.execute(stmt).scalars().first()
            if intent is not None:
                session.expunge(intent)
            return intent

    def get_stale_pending_intents(self, *, now: datetime) -> list[_OrderIntent]:
        """lease_until を過ぎた pending 行を返す (recovery job 用、§8.8)。

        pending のまま lease 超過した行は plan_id UNIQUE を永久に握り発注を
        止めるため、起動時の recovery job がこれらを列挙し submitted_at の有無で
        retryable / needs_reconcile を判定する (復旧本体は later plan)。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_OrderIntent)
                .where(_OrderIntent.status == "pending")
                .where(_OrderIntent.lease_until < now)
            )
            rows = list(session.execute(stmt).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    def mark_order_submitted(self, *, plan_id: int, submitted_at: datetime) -> None:
        """broker 送信直前に submitted_at を埋める (送信前/後の分岐点)。"""
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.plan_id == plan_id)
            intent = session.execute(stmt).scalars().first()
            if intent is None:
                logger.warning(f"mark_order_submitted: plan_id {plan_id} not found")
                return
            intent.submitted_at = submitted_at
            intent.status = "submitted"
            intent.updated_at = db_now()
            session.commit()

    def record_order_result(
        self,
        *,
        plan_id: int,
        status: str,
        order_id: str | None = None,
        broker_result_json: dict | None = None,
    ) -> None:
        """broker 応答で status / order_id / broker_result を更新する。"""
        if status not in ORDER_INTENT_STATUSES:
            raise ValueError(
                f"status must be one of {ORDER_INTENT_STATUSES}, got {status!r}"
            )
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.plan_id == plan_id)
            intent = session.execute(stmt).scalars().first()
            if intent is None:
                logger.warning(f"record_order_result: plan_id {plan_id} not found")
                return
            intent.status = status
            intent.order_id = order_id
            intent.broker_result_json = broker_result_json
            intent.updated_at = db_now()
            session.commit()
