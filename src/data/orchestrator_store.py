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
    JSON, Boolean, Column, DateTime, Float, Integer, String, UniqueConstraint,
    and_, func, or_, select, text, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """JSON カラムに入れる前に datetime 等の非 JSON 値を正規化する。

    dict / list を再帰的に走査し、datetime は ISO 文字列に変換する。SQLAlchemy の
    JSON 型は標準 json エンコーダを使うため、datetime をそのまま渡すと
    'Object of type datetime is not JSON serializable' で保存に失敗する。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class _DecisionSnapshot(_Base):
    """decision 開始時に materialize する入力スナップショット (spec §8.7)。"""
    __tablename__ = "decision_snapshots"

    snapshot_id       = Column(Integer, primary_key=True, autoincrement=True)
    pair              = Column(String, nullable=False, index=True)
    as_of_time        = Column(DateTime, nullable=False)
    quote_json        = Column(JSON)
    technical_ref     = Column(JSON)
    news_ref          = Column(JSON)
    position_json     = Column(JSON)   # P-5: LLM が見た建玉ブロック (検証用)
    current_plan_json = Column(JSON)   # P-5: 同 current_plan (null 可)
    created_at        = Column(DateTime, nullable=False)


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
    # approval gate (spec 2026-07-05 F-1): 承認待ち / 却下 (terminal)
    "pending_approval", "rejected",
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
    scale_in              = Column(Boolean)  # P-2b: 建玉ありでの同方向 plan (null=旧データ)
    new_signal_evidence   = Column(String)   # P-2b: scale_in の新シグナル根拠
    # ── approval gate (spec 2026-07-05 F-2): nullable — gate OFF plan は全て NULL ──
    gate_decision         = Column(String)   # approved | rejected | unanswered (永続ラベル正本)
    gate_decided_at       = Column(DateTime) # 承認/却下の時刻 (放置=unanswered は NULL)
    gate_reason           = Column(String)   # 却下理由 (自由記述・任意)
    gate_message_id       = Column(String)   # Discord message ID (bot 再起動突合)
    # ── 反実仮想追跡 (F-4): stamp → 終端時に記録 ──
    cf_state              = Column(String)   # would_trigger | triggered | invalidated | superseded
    cf_stamped_at         = Column(DateTime) # pending 中の entry 初成立時刻
    cf_stamp_price        = Column(Float)    # 同・成立時 mid
    cf_stamp_spread_pips  = Column(Float)    # 同・成立時 spread (pips、hindsight 採点用)
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


# spec §8.3 の decision_type 集合
DECISION_TYPES = (
    "plan_create", "plan_update", "plan_invalidate",
    "plan_trigger", "direct_hold", "reject",
    # approval gate (F-4): 反実仮想 trigger。real の plan_trigger と集計分離
    "plan_cf_trigger",
)


class _OrchestratorDecision(_Base):
    """Orchestrator ランタイムが記録する判断ライフサイクルイベント (spec §8.3)。"""
    __tablename__ = "orchestrator_decisions"

    decision_id       = Column(Integer, primary_key=True, autoincrement=True)
    run_id            = Column(Integer, index=True)
    snapshot_id       = Column(Integer, index=True)
    pair              = Column(String, nullable=False, index=True)
    decision_type     = Column(String, nullable=False)
    decision          = Column(String)   # buy | sell | hold | skip | reject | null
    plan_id           = Column(Integer)
    final_score       = Column(Float)
    confidence        = Column(Float)
    reasoning_summary = Column(String)
    risk_gate_result  = Column(JSON)
    order_id          = Column(String)
    trade_horizon     = Column(String)
    advice_memo_hash  = Column(String)
    created_at        = Column(DateTime, nullable=False)


class _DecisionVote(_Base):
    """各 agent opinion の監査用 trace。最終判断ではない (spec §8.4)。"""
    __tablename__ = "decision_votes"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    decision_id       = Column(Integer, nullable=False, index=True)
    agent_run_id      = Column(Integer)
    agent_name        = Column(String, nullable=False)
    vote_action       = Column(String)
    vote_score        = Column(Float)
    vote_confidence   = Column(Float)
    reflected_in_plan = Column(Integer)   # 0/1 (bool を SQLite 互換に)


class _DataFreshnessSnapshot(_Base):
    """判断時に見たデータ鮮度 (spec §8.5)。snapshot 起点で紐付ける。"""
    __tablename__ = "data_freshness_snapshots"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id       = Column(Integer, nullable=False, index=True)
    decision_id       = Column(Integer)  # 任意・trace 用 (UNIQUE/必須にしない)
    pair              = Column(String, nullable=False)
    price_age_sec     = Column(Float)
    technical_age_sec = Column(Float)
    news_age_sec      = Column(Float)
    rag_case_count    = Column(Integer)
    issues_json       = Column(JSON)


class _AgentOutput(_Base):
    """agent が出した opinion / summary (spec §8.2)。"""
    __tablename__ = "agent_outputs"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    run_id                 = Column(Integer, nullable=False, index=True)
    agent_name             = Column(String, nullable=False)
    pair                   = Column(String)
    output_type            = Column(String)
    action                 = Column(String)  # buy | sell | hold | skip | advisory
    score                  = Column(Float)
    confidence             = Column(Float)
    reasoning_summary      = Column(String)
    structured_payload_json = Column(JSON)
    observed_at            = Column(DateTime)
    saved_at               = Column(DateTime, nullable=False)
    data_freshness_status  = Column(String)


class _ExecutionOpinion(_Base):
    """ExecutionOpinionAgent の出力専用テーブル (spec §8.6)。"""
    __tablename__ = "execution_opinions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    run_id              = Column(Integer, nullable=False, index=True)
    pair                = Column(String, nullable=False)
    action              = Column(String)
    entry_reference_price = Column(Float)
    sl                  = Column(Float)
    tp                  = Column(Float)
    rr                  = Column(Float)
    invalid_stops_risk  = Column(String)
    bridge_risk         = Column(String)
    comment_risk        = Column(String)
    reasoning_summary   = Column(String)


class _ShadowTrigger(_Base):
    """watch loop が shadow で記録する plan_trigger イベント (spec §8.1)。

    Phase 2/3 の判断品質検証の主対象。trigger と outcome は後続分析の主対象なので
    orchestrator_decisions.risk_gate_result に詰め込まず独立 table にする (§8.1)。
    """
    __tablename__ = "shadow_triggers"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    plan_id               = Column(Integer, nullable=False, index=True)
    decision_id           = Column(Integer)
    pair                  = Column(String, nullable=False)
    direction             = Column(String)   # long | short
    triggered_at          = Column(DateTime, nullable=False)
    trigger_price         = Column(Float)
    sl                    = Column(Float)
    tp                    = Column(Float)
    rr                    = Column(Float)
    spread_pips           = Column(Float)   # trigger 時の spread (spec 2026-07-05 S-5)
    snapshot_id           = Column(Integer)
    risk_gate_result_json = Column(JSON)

    # plan ごとに shadow trigger は最大 1 回 (Phase 2 active plan = 1)。原子 claim を
    # 擦り抜けても重複行を DB が拒否する defense-in-depth (Codex High#1)。
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_shadow_triggers_plan_id"),
    )


# shadow_hindsight_evaluations.status の集合 (spec §8.1)
HINDSIGHT_STATUSES = ("pending", "evaluated", "failed")


class _ShadowHindsightEvaluation(_Base):
    """trigger 後の判断品質を後追い計測する outcome テーブル (spec §8.1 / Phase 4)。

    Plan C: trigger 確定時に status='pending' で 1 行 enqueue し、hindsight poll loop が
    horizon_seconds 経過後に MFE-R/MAE-R/PnL-R を埋めて 'evaluated'/'failed' に遷移する。
    trigger と outcome は後続分析の主対象なので独立 table にする (§8.1)。
    """
    __tablename__ = "shadow_hindsight_evaluations"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    shadow_trigger_id = Column(Integer, nullable=False, index=True)
    evaluated_at      = Column(DateTime)   # null = まだ評価していない (pending)
    status            = Column(String, nullable=False)  # pending | evaluated | failed
    horizon_seconds   = Column(Integer)
    mfe_r             = Column(Float)
    mae_r             = Column(Float)
    # NET (spread 控除後)。spread_cost_r が NULL の行は GROSS (旧データ or spread 不明)。
    # gross = pnl_r + spread_cost_r で復元可能 (spec D-8)。
    pnl_r             = Column(Float)
    would_hit_sl      = Column(Integer)    # 0/1 (bool を SQLite 互換に)
    would_hit_tp      = Column(Integer)    # 0/1
    reasoning_summary = Column(String)
    created_at        = Column(DateTime, nullable=False)
    spread_cost_r     = Column(Float)   # R 換算の spread コスト内訳 (spec 2026-07-05 S-5)

    # trigger ごとに hindsight 行は最大 1 件 (二重 enqueue を DB で防ぐ)。
    __table_args__ = (
        UniqueConstraint(
            "shadow_trigger_id", name="uq_shadow_hindsight_trigger_id"
        ),
    )


class _ProtectionDecision(_Base):
    """保護判定の記録 (price_monitor / tick_worker 並走比較用、spec §5.4)。"""
    __tablename__ = "protection_decisions"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    ts         = Column(DateTime, nullable=False, index=True)
    pair       = Column(String, nullable=False, index=True)
    order_id   = Column(String, nullable=False, index=True)
    source     = Column(String, nullable=False)  # price_monitor | tick_worker
    action     = Column(String, nullable=False)  # none | raise_sl | close
    stage      = Column(String)                  # half | breakeven | lock | null
    target_sl  = Column(Float)
    mfe_r      = Column(Float)
    giveback_r = Column(Float)


class OrchestratorStore:
    """spec §8 のテーブル群を 1 つの DB で管理するストア。"""

    def __init__(self, db_path: Path) -> None:
        self._engine = _get_engine(db_path)
        self._migrate()

    def _migrate(self) -> None:
        """既存テーブルに新カラムを追加する (ALTER TABLE、既にあれば何もしない)。"""
        migrations = [
            ("shadow_triggers", "spread_pips", "FLOAT"),
            ("shadow_hindsight_evaluations", "spread_cost_r", "FLOAT"),
            ("decision_snapshots", "position_json", "JSON"),
            ("decision_snapshots", "current_plan_json", "JSON"),
            ("trade_plans", "scale_in", "BOOLEAN"),
            ("trade_plans", "new_signal_evidence", "VARCHAR"),
            # approval gate (spec 2026-07-05 F-2)
            ("trade_plans", "gate_decision", "VARCHAR"),
            ("trade_plans", "gate_decided_at", "DATETIME"),
            ("trade_plans", "gate_reason", "VARCHAR"),
            ("trade_plans", "gate_message_id", "VARCHAR"),
            ("trade_plans", "cf_state", "VARCHAR"),
            ("trade_plans", "cf_stamped_at", "DATETIME"),
            ("trade_plans", "cf_stamp_price", "FLOAT"),
            ("trade_plans", "cf_stamp_spread_pips", "FLOAT"),
        ]
        with self._engine.connect() as conn:
            for table, col, col_type in migrations:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
                    logger.info(f"[MIGRATE] Added {table}.{col}")
                except Exception:
                    pass  # カラムが既に存在

    # ── decision_snapshots (§8.7) ──────────────────────────────

    def create_snapshot(
        self,
        pair: str,
        as_of_time: datetime,
        quote_json: dict[str, Any] | None = None,
        technical_ref: dict[str, Any] | None = None,
        news_ref: dict[str, Any] | None = None,
        position_json: dict[str, Any] | None = None,
        current_plan_json: dict[str, Any] | None = None,
    ) -> int:
        """decision_snapshot を materialize し snapshot_id を返す。"""
        with Session(self._engine) as session:
            snap = _DecisionSnapshot(
                pair=pair,
                as_of_time=as_of_time,
                quote_json=quote_json,
                technical_ref=technical_ref,
                news_ref=news_ref,
                position_json=position_json,
                current_plan_json=current_plan_json,
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

    def attach_snapshot(self, run_id: int, snapshot_id: int) -> None:
        """run に decision_snapshot を後付けで紐付ける (§8.1 trace graph)。

        start_run は snapshot 作成前に呼ぶ (失敗時も failed run を残すため)。
        DecisionContextBuilder.build で snapshot を materialize した後に本メソッドで
        agent_runs.snapshot_id を埋め、agent_run → decision_snapshot のトレースを繋ぐ。
        """
        with Session(self._engine) as session:
            run = session.get(_AgentRun, run_id)
            if run is None:
                logger.warning(f"attach_snapshot: run_id {run_id} not found")
                return
            run.snapshot_id = snapshot_id
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
        status: str = "active",
        scale_in: bool | None = None,
        new_signal_evidence: str | None = None,
    ) -> int:
        """trade_plan を作成し plan_id を返す。

        status を指定可能 (既定 active)。pipeline は orphan 防止のため一旦
        'requires_replan' で作り、decision/vote 記録後に update_plan_status('active')
        で昇格する (Codex High#2)。get_active_plans は active のみ拾うので、途中失敗時に
        decision 無しの active plan が残らない。
        """
        if status not in PLAN_STATUSES:
            raise ValueError(f"status must be one of {PLAN_STATUSES}, got {status!r}")
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
                status=status,
                created_by_run_id=created_by_run_id,
                scale_in=scale_in,
                new_signal_evidence=new_signal_evidence,
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

    def try_claim_plan_status(
        self, plan_id: int, new_status: str, *, from_status: str = "active",
    ) -> bool:
        """plan を from_status のときだけ new_status に進める原子的 claim。

        `UPDATE ... WHERE plan_id=? AND status=from_status` の rowcount で判定する。
        SQLite では SELECT ... FOR UPDATE が発行されない (with_for_update は no-op) ため、
        select→update の TOCTOU 隙間が残る。単一文の条件付き UPDATE なら 1 行更新が
        原子的になり、並行 watch / planning の二重遷移・stale 上書きを防ぐ
        (Codex High#1/#2)。勝てば True、既に from_status でなければ False。
        """
        if new_status not in PLAN_STATUSES:
            raise ValueError(f"status must be one of {PLAN_STATUSES}, got {new_status!r}")
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == from_status)
                .values(status=new_status, updated_at=db_now())
            )
            session.commit()
            return result.rowcount == 1

    def try_mark_plan_triggered(self, plan_id: int) -> bool:
        """plan を active のときだけ triggered に進める (条件付き原子 claim)。

        get_active_plans → 状態遷移の TOCTOU で同一 plan が二重 trigger されるのを防ぐ。
        既に非 active (triggered/superseded/invalidated 等) なら False を返す。
        """
        return self.try_claim_plan_status(plan_id, "triggered", from_status="active")

    def try_decide_gate(
        self, plan_id: int, decision: str, *, reason: str | None = None,
    ) -> bool:
        """pending_approval の plan に承認/却下を原子的に確定する (gate spec F-2b)。

        status 遷移 (approved→active / rejected→rejected) と gate_decision /
        gate_decided_at / gate_reason を**単一の条件付き UPDATE** で残す。
        try_claim_plan_status では label/時刻/理由を同一 tx で残せないため専用化
        (codex Medium)。二重クリック・承認と却下の競合は rowcount 排他で自然解決 —
        勝てば True、既に pending でなければ False (API 層で 409 に写像)。
        """
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"decision must be 'approved' or 'rejected', got {decision!r}"
            )
        new_status = "active" if decision == "approved" else "rejected"
        now = db_now()
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .values(
                    status=new_status, gate_decision=decision,
                    gate_decided_at=now, gate_reason=reason, updated_at=now,
                )
            )
            session.commit()
            return result.rowcount == 1

    def try_close_pending_unanswered(self, plan_id: int, to_status: str) -> bool:
        """pending_approval を人間の判断なしに終端する (gate spec F-1/F-2b)。

        status (expired=TTL 満了 / invalidated=構造死) と gate_decision='unanswered'
        を単一 UPDATE で刻印。approve/reject との race は rowcount 排他で自然解決。
        gate_decided_at は NULL のまま (放置に決定時刻はない)。
        """
        if to_status not in ("expired", "invalidated"):
            raise ValueError(
                f"to_status must be 'expired' or 'invalidated', got {to_status!r}"
            )
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .values(
                    status=to_status, gate_decision="unanswered",
                    updated_at=db_now(),
                )
            )
            session.commit()
            return result.rowcount == 1

    def try_stamp_would_trigger(
        self, plan_id: int, *, at: datetime, price: float,
        spread_pips: float | None,
    ) -> bool:
        """pending 中の entry 初成立を plan 行に stamp する (gate spec F-4)。

        shadow_triggers には**書かない** — UNIQUE(plan_id) 制約下で、後に承認され
        real trigger が来た時の衝突を構造的に回避する (stamp→終端時に記録 方式)。
        cf_state IS NULL 条件の rowcount claim で初回のみ成立 (dedupe =
        「最初に条件成立した瞬間」がエントリー点)。
        """
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .where(_TradePlan.cf_state.is_(None))
                .values(
                    cf_state="would_trigger", cf_stamped_at=at,
                    cf_stamp_price=price, cf_stamp_spread_pips=spread_pips,
                    updated_at=db_now(),
                )
            )
            session.commit()
            return result.rowcount == 1

    def try_latch_cf_invalidated(self, plan_id: int) -> bool:
        """rejected plan の反実仮想追跡窓を閉じる latch (gate spec F-4)。

        rejected は status が動かないため、invalidation 成立を cf_state='invalidated'
        で記録し以後の entry 評価を止める。latch なしだと「invalidation 成立 →
        価格戻り → entry 成立」で、承認世界なら invalidation で死んでいた plan に
        cf 行が付く (誤った反実仮想)。
        """
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "rejected")
                .where(_TradePlan.cf_state.is_(None))
                .values(cf_state="invalidated", updated_at=db_now())
            )
            session.commit()
            return result.rowcount == 1

    def finalize_cf_trigger(
        self,
        plan_id: int,
        *,
        run_id: int,
        enqueue_hindsight: bool,
        horizon_seconds: int,
        triggered_at: datetime | None = None,
        trigger_price: float | None = None,
        spread_pips: float | None = None,
        snapshot_id: int | None = None,
        reasoning_summary: str = "counterfactual trigger",
    ) -> int | None:
        """反実仮想 trigger 行を単一 transaction で確定する (gate spec F-2b/F-4)。

        (1) cf_state → 'triggered' の rowcount claim、(2) plan_cf_trigger decision
        INSERT、(3) shadow_triggers INSERT、(4) hindsight enqueue、を 1 commit で行う。
        個別 commit の record_* 群は流用しない — 中間クラッシュで「cf_state だけ進んで
        行がない」孤児や、claim 負け時の偽 decision が出る (codex plan review High#2)。

        claim の許可条件 (承認済み plan の保護 — codex plan review High#1):
        - stamp 済み終端 (would_trigger→): gate_decision IN ('rejected','unanswered')
        - stamp なし直接記録 (NULL→): status='rejected' AND gate_decision='rejected'
        approved plan は real trigger 後に live 発注失敗で invalidated になり得る
        (runtime の is_executed=False 経路) が、gate_decision='approved' なので
        claim が構造的に成立しない。

        triggered_at/trigger_price/spread_pips 省略時は stamp 値。claim 負けは None。
        UNIQUE(plan_id) 違反は**状態を進めず** rollback して None (整合性エラーとして
        ログ — 無条件前進は孤児を再発させる)。
        """
        with Session(self._engine) as session:
            plan = session.get(_TradePlan, plan_id)
            if plan is None:
                logger.warning(f"finalize_cf_trigger: plan {plan_id} not found")
                return None
            at = triggered_at if triggered_at is not None else plan.cf_stamped_at
            price = trigger_price if trigger_price is not None else plan.cf_stamp_price
            spread = spread_pips if spread_pips is not None else plan.cf_stamp_spread_pips
            if at is None:
                logger.warning(
                    f"finalize_cf_trigger: plan {plan_id} has neither stamp nor args"
                )
                return None
            action = plan.action_json if isinstance(plan.action_json, dict) else {}
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status.in_(("rejected", "expired", "invalidated")))
                .where(or_(
                    and_(
                        _TradePlan.cf_state == "would_trigger",
                        _TradePlan.gate_decision.in_(("rejected", "unanswered")),
                    ),
                    and_(
                        _TradePlan.cf_state.is_(None),
                        _TradePlan.status == "rejected",
                        _TradePlan.gate_decision == "rejected",
                    ),
                ))
                .values(cf_state="triggered", updated_at=db_now())
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            dec = _OrchestratorDecision(
                run_id=run_id, snapshot_id=plan.snapshot_id, pair=plan.pair,
                decision_type="plan_cf_trigger",
                decision="buy" if plan.direction == "long" else "sell",
                plan_id=plan_id, reasoning_summary=reasoning_summary,
                trade_horizon=plan.horizon, created_at=db_now(),
            )
            session.add(dec)
            try:
                session.flush()  # decision_id 採番
                trig = _ShadowTrigger(
                    plan_id=plan_id, decision_id=dec.decision_id, pair=plan.pair,
                    direction=plan.direction, triggered_at=at, trigger_price=price,
                    sl=action.get("sl"), tp=action.get("tp"), rr=action.get("rr"),
                    spread_pips=spread, snapshot_id=snapshot_id,
                    risk_gate_result_json=None,
                )
                session.add(trig)
                session.flush()  # UNIQUE(plan_id) をここで検出 (commit 前)
            except IntegrityError as exc:
                session.rollback()
                # 行が既に存在する病的状態。状態を進めず整合性エラーとして残す
                # (無条件で cf_state を進めると孤児を再発させる — codex High#2)。
                logger.error(
                    f"[ORCH] finalize_cf_trigger integrity conflict for plan "
                    f"{plan_id}: {getattr(exc, 'orig', exc)}"
                )
                return None
            if enqueue_hindsight:
                session.add(_ShadowHindsightEvaluation(
                    shadow_trigger_id=trig.id, evaluated_at=None, status="pending",
                    horizon_seconds=horizon_seconds, created_at=db_now(),
                ))
            session.commit()
            logger.info(
                f"[ORCH] cf trigger finalized: plan {plan_id} ({plan.pair}) @ {price}"
            )
            return trig.id

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

    def get_plans_by_status(
        self, statuses: tuple[str, ...], pair: str | None = None,
    ) -> list[_TradePlan]:
        """指定 status 群の plan を created_at 降順で返す (pair 指定で絞り込み)。

        get_active_plans と違い任意 status を引ける汎用メソッド。承認ゲート
        (pending_approval) 表示や F-5 API がこれを共用する。空 statuses は
        in_(()) の DB 依存を避けて空リストを返す。
        """
        if not statuses:
            return []
        with Session(self._engine) as session:
            stmt = select(_TradePlan).where(_TradePlan.status.in_(statuses))
            if pair is not None:
                stmt = stmt.where(_TradePlan.pair == pair)
            stmt = stmt.order_by(_TradePlan.created_at.desc())
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

    def get_stale_or_unconfirmed_intents(self, *, now: datetime) -> list["_OrderIntent"]:
        """recovery 候補を返す (Task F, codex #1)。lease 超過のうち未完了 = recovery 3 分岐:
        status=="pending" (未送信) OR status=="submitted" (送信後)。後者は order_id 有無で
        recovery job が needs_reconcile / filled 補正に分岐する。terminal (filled/rejected/
        failed/abandoned) は対象外。'pending only' の既存 get_stale_pending_intents だと
        submitted (送信直後クラッシュ) を取りこぼすため別メソッドにする。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_OrderIntent)
                .where(_OrderIntent.lease_until < now)
                .where(
                    or_(
                        _OrderIntent.status == "pending",
                        _OrderIntent.status == "submitted",
                    )
                )
            )
            rows = list(session.execute(stmt).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    def set_recovery_status(self, *, plan_id: int, recovery_status: str) -> None:
        """recovery job が判定した recovery_status を書き込む (Task F)。"""
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.plan_id == plan_id)
            intent = session.execute(stmt).scalars().first()
            if intent is None:
                logger.warning(f"set_recovery_status: plan_id {plan_id} not found")
                return
            intent.recovery_status = recovery_status
            intent.updated_at = db_now()
            session.commit()

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

    # ── orchestrator_decisions (§8.3) ──────────────────────────

    def record_decision(
        self,
        *,
        run_id: int,
        snapshot_id: int,
        pair: str,
        decision_type: str,
        decision: str | None = None,
        plan_id: int | None = None,
        final_score: float | None = None,
        confidence: float | None = None,
        reasoning_summary: str | None = None,
        risk_gate_result: dict | None = None,
        order_id: str | None = None,
        trade_horizon: str | None = None,
        advice_memo_hash: str | None = None,
    ) -> int:
        if decision_type not in DECISION_TYPES:
            raise ValueError(
                f"decision_type must be one of {DECISION_TYPES}, got {decision_type!r}"
            )
        with Session(self._engine) as session:
            dec = _OrchestratorDecision(
                run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                decision_type=decision_type, decision=decision, plan_id=plan_id,
                final_score=final_score, confidence=confidence,
                reasoning_summary=reasoning_summary, risk_gate_result=risk_gate_result,
                order_id=order_id, trade_horizon=trade_horizon,
                advice_memo_hash=advice_memo_hash, created_at=db_now(),
            )
            session.add(dec)
            session.commit()
            return dec.decision_id

    def get_decision(self, decision_id: int) -> _OrchestratorDecision | None:
        with Session(self._engine) as session:
            dec = session.get(_OrchestratorDecision, decision_id)
            if dec is not None:
                session.expunge(dec)
            return dec

    # ── data_freshness_snapshots (§8.5) ────────────────────────

    def record_freshness(
        self,
        *,
        snapshot_id: int,
        pair: str,
        price_age_sec: float | None = None,
        technical_age_sec: float | None = None,
        news_age_sec: float | None = None,
        rag_case_count: int | None = None,
        issues: list | None = None,
        decision_id: int | None = None,
    ) -> int:
        with Session(self._engine) as session:
            row = _DataFreshnessSnapshot(
                snapshot_id=snapshot_id, decision_id=decision_id, pair=pair,
                price_age_sec=price_age_sec, technical_age_sec=technical_age_sec,
                news_age_sec=news_age_sec, rag_case_count=rag_case_count,
                issues_json=issues or [],
            )
            session.add(row)
            session.commit()
            return row.id

    def get_freshness_for_snapshot(self, snapshot_id: int) -> _DataFreshnessSnapshot | None:
        with Session(self._engine) as session:
            stmt = (
                select(_DataFreshnessSnapshot)
                .where(_DataFreshnessSnapshot.snapshot_id == snapshot_id)
                .order_by(_DataFreshnessSnapshot.id.desc())
            )
            row = session.execute(stmt).scalars().first()
            if row is not None:
                session.expunge(row)
            return row

    # ── decision_votes (§8.4) ──────────────────────────────────

    def record_vote(
        self,
        *,
        decision_id: int,
        agent_run_id: int,
        agent_name: str,
        vote_action: str | None = None,
        vote_score: float | None = None,
        vote_confidence: float | None = None,
        reflected_in_plan: bool = False,
    ) -> int:
        with Session(self._engine) as session:
            vote = _DecisionVote(
                decision_id=decision_id, agent_run_id=agent_run_id,
                agent_name=agent_name, vote_action=vote_action,
                vote_score=vote_score, vote_confidence=vote_confidence,
                reflected_in_plan=1 if reflected_in_plan else 0,
            )
            session.add(vote)
            session.commit()
            return vote.id

    def get_votes(self, decision_id: int) -> list[_DecisionVote]:
        """decision の vote 群を返す。reflected_in_plan は bool に正規化。"""
        with Session(self._engine) as session:
            stmt = select(_DecisionVote).where(_DecisionVote.decision_id == decision_id)
            votes = list(session.execute(stmt).scalars().all())
            for v in votes:
                v.reflected_in_plan = bool(v.reflected_in_plan)
                session.expunge(v)
            return votes

    # ── agent_outputs (§8.2) ───────────────────────────────────

    def record_agent_output(
        self,
        *,
        run_id: int,
        agent_name: str,
        pair: str | None = None,
        output_type: str | None = None,
        action: str | None = None,
        score: float | None = None,
        confidence: float | None = None,
        reasoning_summary: str | None = None,
        structured_payload: dict | None = None,
        observed_at: datetime | None = None,
        data_freshness_status: str | None = None,
    ) -> int:
        """agent_outputs に 1 行書き、新規 id を返す。saved_at は db_now()。

        structured_payload は structured_payload_json カラムに入れる。datetime 等の
        非 JSON 値は ISO 文字列に正規化してから保存する (datetime is not JSON serializable
        で保存に失敗しないため)。
        """
        with Session(self._engine) as session:
            out = _AgentOutput(
                run_id=run_id,
                agent_name=agent_name,
                pair=pair,
                output_type=output_type,
                action=action,
                score=score,
                confidence=confidence,
                reasoning_summary=reasoning_summary,
                structured_payload_json=_json_safe(structured_payload),
                observed_at=observed_at,
                saved_at=db_now(),
                data_freshness_status=data_freshness_status,
            )
            session.add(out)
            session.commit()
            return out.id

    def get_agent_outputs(self, run_id: int) -> list[_AgentOutput]:
        """run_id の agent_outputs を id 昇順で返す。"""
        with Session(self._engine) as session:
            stmt = (
                select(_AgentOutput)
                .where(_AgentOutput.run_id == run_id)
                .order_by(_AgentOutput.id.asc())
            )
            rows = list(session.execute(stmt).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    # ── execution_opinions (§8.6) ──────────────────────────────

    def record_execution_opinion(
        self,
        *,
        run_id: int,
        pair: str,
        action: str | None = None,
        entry_reference_price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        rr: float | None = None,
        invalid_stops_risk: str | None = None,
        bridge_risk: str | None = None,
        comment_risk: str | None = None,
        reasoning_summary: str | None = None,
    ) -> int:
        """execution_opinions に 1 行書き、新規 id を返す。"""
        with Session(self._engine) as session:
            op = _ExecutionOpinion(
                run_id=run_id,
                pair=pair,
                action=action,
                entry_reference_price=entry_reference_price,
                sl=sl,
                tp=tp,
                rr=rr,
                invalid_stops_risk=invalid_stops_risk,
                bridge_risk=bridge_risk,
                comment_risk=comment_risk,
                reasoning_summary=reasoning_summary,
            )
            session.add(op)
            session.commit()
            return op.id

    def get_execution_opinion(self, run_id: int) -> _ExecutionOpinion | None:
        """run_id の最新 execution_opinion を返す (無ければ None)。

        同 run_id に複数あれば最大 id (= 最新) を返す。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_ExecutionOpinion)
                .where(_ExecutionOpinion.run_id == run_id)
                .order_by(_ExecutionOpinion.id.desc())
            )
            op = session.execute(stmt).scalars().first()
            if op is not None:
                session.expunge(op)
            return op

    # ── shadow_triggers (§8.1) ─────────────────────────────────

    def record_shadow_trigger(
        self,
        *,
        plan_id: int,
        decision_id: int | None,
        pair: str,
        direction: str | None,
        triggered_at: datetime,
        trigger_price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        rr: float | None = None,
        snapshot_id: int | None = None,
        risk_gate_result: dict | None = None,
        spread_pips: float | None = None,
    ) -> int:
        """shadow_triggers に 1 行書き、新規 id を返す (§8.1)。

        watch loop が plan_trigger を shadow 記録した直後に呼ぶ。risk_gate_result は
        JSON 安全な dict (RiskGateResult.to_dict()) を渡す。spread_pips は trigger 時点の
        spread (pips 換算)。hindsight の spread コスト採点に使う (spec S-5)。
        """
        with Session(self._engine) as session:
            trig = _ShadowTrigger(
                plan_id=plan_id,
                decision_id=decision_id,
                pair=pair,
                direction=direction,
                triggered_at=triggered_at,
                trigger_price=trigger_price,
                sl=sl,
                tp=tp,
                rr=rr,
                spread_pips=spread_pips,
                snapshot_id=snapshot_id,
                risk_gate_result_json=_json_safe(risk_gate_result),
            )
            session.add(trig)
            session.commit()
            return trig.id

    def get_shadow_trigger_by_id(self, trigger_id: int) -> _ShadowTrigger | None:
        """主キー id で shadow_trigger を引く (hindsight poll 用)。"""
        with Session(self._engine) as session:
            trig = session.get(_ShadowTrigger, trigger_id)
            if trig is not None:
                session.expunge(trig)
            return trig

    def get_shadow_trigger(self, plan_id: int) -> _ShadowTrigger | None:
        """plan_id の最新 shadow_trigger を返す (無ければ None)。

        Phase 2 は pair ごとに active plan 1 件・trigger 1 回が原則だが、
        同 plan に複数あれば最大 id (= 最新) を返す。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_ShadowTrigger)
                .where(_ShadowTrigger.plan_id == plan_id)
                .order_by(_ShadowTrigger.id.desc())
            )
            trig = session.execute(stmt).scalars().first()
            if trig is not None:
                session.expunge(trig)
            return trig

    # ── shadow_hindsight_evaluations (§8.1 / Phase 4) ──────────

    def record_hindsight_evaluation(
        self,
        *,
        shadow_trigger_id: int,
        horizon_seconds: int,
    ) -> int:
        """trigger 確定時に pending hindsight 行を 1 件 enqueue し id を返す (Plan C)。

        metric (mfe_r 等) はこの時点では None。horizon_seconds 経過後に poll loop が
        update_hindsight_evaluation で埋める。shadow_trigger_id UNIQUE で二重 enqueue を防ぐ。

        この呼び出しは trigger 記録パスの一部なので、重複時に例外を投げると trigger
        記録ごと巻き戻る。UNIQUE 違反は「既に enqueue 済み」として冪等に既存行の id を返す
        (record_shadow_trigger と同じ IntegrityError ハンドリング思想)。
        """
        with Session(self._engine) as session:
            ev = _ShadowHindsightEvaluation(
                shadow_trigger_id=shadow_trigger_id,
                evaluated_at=None,
                status="pending",
                horizon_seconds=horizon_seconds,
                created_at=db_now(),
            )
            session.add(ev)
            try:
                session.commit()
                return ev.id
            except IntegrityError as exc:
                session.rollback()
                orig = str(getattr(exc, "orig", exc))
                if (
                    "uq_shadow_hindsight_trigger_id" in orig
                    or "shadow_hindsight_evaluations.shadow_trigger_id" in orig
                ):
                    existing = self.get_hindsight_evaluation(shadow_trigger_id)
                    if existing is not None:
                        logger.info(
                            f"hindsight enqueue skipped: shadow_trigger {shadow_trigger_id} "
                            "already has an evaluation"
                        )
                        return existing.id
                # 想定外の IntegrityError は握り潰さず再送出 (silent failure 防止)。
                raise

    def get_hindsight_evaluation(
        self, shadow_trigger_id: int
    ) -> _ShadowHindsightEvaluation | None:
        """shadow_trigger_id の最新 hindsight 行を返す (would_hit_* は bool 正規化)。"""
        with Session(self._engine) as session:
            stmt = (
                select(_ShadowHindsightEvaluation)
                .where(
                    _ShadowHindsightEvaluation.shadow_trigger_id == shadow_trigger_id
                )
                .order_by(_ShadowHindsightEvaluation.id.desc())
            )
            ev = session.execute(stmt).scalars().first()
            if ev is not None:
                ev.would_hit_sl = None if ev.would_hit_sl is None else bool(ev.would_hit_sl)
                ev.would_hit_tp = None if ev.would_hit_tp is None else bool(ev.would_hit_tp)
                session.expunge(ev)
            return ev

    def update_hindsight_evaluation(
        self,
        hindsight_id: int,
        *,
        status: str,
        evaluated_at: datetime,
        mfe_r: float | None = None,
        mae_r: float | None = None,
        pnl_r: float | None = None,
        would_hit_sl: bool | None = None,
        would_hit_tp: bool | None = None,
        reasoning_summary: str | None = None,
        spread_cost_r: float | None = None,
    ) -> None:
        """poll loop が pending 行に metric を埋め status を遷移させる。

        評価成功なら status='evaluated' + metric 群、評価不能なら status='failed'。
        spread_cost_r は pnl_r (NET) から控除した spread コストの内訳 (spec D-8)。
        """
        if status not in HINDSIGHT_STATUSES:
            raise ValueError(
                f"status must be one of {HINDSIGHT_STATUSES}, got {status!r}"
            )
        with Session(self._engine) as session:
            ev = session.get(_ShadowHindsightEvaluation, hindsight_id)
            if ev is None:
                logger.warning(
                    f"update_hindsight_evaluation: id {hindsight_id} not found"
                )
                return
            ev.status = status
            ev.evaluated_at = evaluated_at
            ev.mfe_r = mfe_r
            ev.mae_r = mae_r
            ev.pnl_r = pnl_r
            ev.would_hit_sl = None if would_hit_sl is None else int(would_hit_sl)
            ev.would_hit_tp = None if would_hit_tp is None else int(would_hit_tp)
            ev.reasoning_summary = reasoning_summary
            ev.spread_cost_r = spread_cost_r
            session.commit()

    def get_pending_hindsight_evaluations(
        self, *, now: datetime
    ) -> list[_ShadowHindsightEvaluation]:
        """評価期限 (triggered_at + horizon_seconds <= now) を過ぎた pending 行を返す。

        shadow_triggers と LEFT join し triggered_at を引く。SQLite には interval 演算が
        無いため Python 側で now - triggered_at の経過秒を horizon_seconds と比較する。
        evaluated/failed 済みは status='pending' フィルタで自然に除外される。

        outer join にするのは、shadow_trigger が見つからない孤児 pending 行も返すため。
        inner join だと孤児は永遠に拾われず pending のまま残る。孤児 (triggered_at=None) は
        評価期限到達済みとして返し、呼び出し側 (runtime) が failed に倒す。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_ShadowHindsightEvaluation, _ShadowTrigger.triggered_at)
                .outerjoin(
                    _ShadowTrigger,
                    _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                )
                .where(_ShadowHindsightEvaluation.status == "pending")
            )
            ready: list[_ShadowHindsightEvaluation] = []
            for ev, triggered_at in session.execute(stmt).all():
                if triggered_at is None:
                    # 孤児 pending (対応する shadow_trigger 無し)。評価対象として返し、
                    # runtime 側で failed に倒させる (pending のまま残さない)。
                    session.expunge(ev)
                    ready.append(ev)
                    continue
                elapsed = (now - triggered_at).total_seconds()
                if elapsed >= (ev.horizon_seconds or 0):
                    session.expunge(ev)
                    ready.append(ev)
            return ready

    # ── shadow metrics 集計 (§8.2 / Phase 4 Task 4.3) ──────────

    def get_shadow_metrics_raw(
        self, *, trade_horizon: str | None = None,
    ) -> dict[str, Any]:
        """shadow 検証 metric の生カウント / 合計を 1 dict で返す。

        rate / 平均などの導出は shadow_metrics.compute_shadow_metrics 側で行う
        (SQL は engine を持つ store に集約し、shaping は pure module に分離)。

        `trade_horizon` を渡すと plan_counts / agent_runs / hindsight 集計を
        その horizon (day | swing) に絞る (daily summary で swing/day 混在の
        gross/net が混ざらないようにする, codex Low)。None なら従来通り全期間
        集計 (後方互換)。
        """
        with Session(self._engine) as session:
            # plan lifecycle: status 別件数
            plan_stmt = select(_TradePlan.status, func.count()).group_by(_TradePlan.status)
            if trade_horizon is not None:
                plan_stmt = plan_stmt.where(_TradePlan.horizon == trade_horizon)
            plan_rows = session.execute(plan_stmt).all()
            plan_counts = {status: count for status, count in plan_rows}

            # agent_runs: status 別件数 (LLM failure rate 用)
            run_stmt = select(_AgentRun.status, func.count()).group_by(_AgentRun.status)
            if trade_horizon is not None:
                run_stmt = run_stmt.where(_AgentRun.trade_horizon == trade_horizon)
            run_rows = session.execute(run_stmt).all()
            run_counts = {status: count for status, count in run_rows}

            # hindsight: status 別件数 + evaluated 行の集計。horizon 絞り込みは
            # shadow_hindsight_evaluations -> shadow_triggers -> trade_plans の
            # join 経由 (hindsight/trigger 自体に horizon 列が無いため)。
            hs_stmt = select(
                _ShadowHindsightEvaluation.status, func.count()
            )
            evaluated_stmt = select(
                func.avg(_ShadowHindsightEvaluation.mfe_r),
                func.avg(_ShadowHindsightEvaluation.mae_r),
                func.avg(_ShadowHindsightEvaluation.pnl_r),
                func.sum(_ShadowHindsightEvaluation.would_hit_sl),
                func.sum(_ShadowHindsightEvaluation.would_hit_tp),
            ).where(_ShadowHindsightEvaluation.status == "evaluated")
            if trade_horizon is not None:
                hs_stmt = (
                    hs_stmt
                    .join(
                        _ShadowTrigger,
                        _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                    )
                    .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                    .where(_TradePlan.horizon == trade_horizon)
                )
                evaluated_stmt = (
                    evaluated_stmt
                    .join(
                        _ShadowTrigger,
                        _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                    )
                    .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                    .where(_TradePlan.horizon == trade_horizon)
                )
            hs_stmt = hs_stmt.group_by(_ShadowHindsightEvaluation.status)
            hs_rows = session.execute(hs_stmt).all()
            hindsight_counts = {status: count for status, count in hs_rows}

            evaluated = session.execute(evaluated_stmt).one()

            # freshness block: issues_json が非空の行数。JSON 比較は型差が大きいため
            # Python 側で判定する (data_freshness_snapshots は小規模テーブル)。
            # horizon 列を持たないため trade_horizon 指定時も絞り込まない (全期間のまま)。
            issues_rows = session.execute(
                select(_DataFreshnessSnapshot.issues_json)
            ).all()
            freshness_blocks = sum(1 for (issues,) in issues_rows if issues)

        return {
            "plan_counts": plan_counts,
            "run_counts": run_counts,
            "hindsight_counts": hindsight_counts,
            "avg_mfe_r": evaluated[0],
            "avg_mae_r": evaluated[1],
            "avg_pnl_r": evaluated[2],
            "sl_hits": int(evaluated[3] or 0),
            "tp_hits": int(evaluated[4] or 0),
            "freshness_blocks": freshness_blocks,
        }

    # ── plan supersede helper (§8.9) ───────────────────────────

    def supersede_active_plans(
        self,
        pair: str,
        *,
        except_plan_id: int | None = None,
        reason: str = "superseded",
    ) -> list[int]:
        """pair の active plan を全て status='superseded' にする。

        except_plan_id は除外する。pair 単位で active plan を最大 1 件に保つために
        使う。superseded にした plan_id のリストを返す。reason はログ用 (テーブルに
        reason 列が無いため status のみ変更し logger.info に残す)。
        """
        # 読み取りと更新を 1 transaction にまとめ、status='active' 条件付きで UPDATE する。
        # こうしないと get_active_plans → update の隙に watch loop が triggered/invalidated に
        # した plan を無条件に superseded で上書きし、trigger 記録や lifecycle metric を壊す。
        with Session(self._engine) as session:
            stmt = (
                select(_TradePlan)
                .where(_TradePlan.pair == pair)
                .where(_TradePlan.status == "active")
                .with_for_update()
            )
            if except_plan_id is not None:
                stmt = stmt.where(_TradePlan.plan_id != except_plan_id)
            plans = list(session.execute(stmt).scalars().all())
            now = db_now()
            ids = []
            for plan in plans:
                plan.status = "superseded"
                plan.updated_at = now
                ids.append(plan.plan_id)
            session.commit()
        logger.info(
            f"[ORCH] superseded {len(ids)} active plan(s) for {pair}: {reason}"
        )
        return ids

    # ── protection_decisions (§5.4) ────────────────────────────

    def record_protection_decision(
        self,
        *,
        ts: datetime,
        pair: str,
        order_id: str,
        source: str,
        action: str,
        stage: str | None,
        target_sl: float | None,
        mfe_r: float | None,
        giveback_r: float | None,
    ) -> None:
        """保護判定を 1 件記録する (spec §5.4)。実クローズ/SL更新とは独立。"""
        with Session(self._engine) as session:
            session.add(_ProtectionDecision(
                ts=ts, pair=pair, order_id=order_id, source=source,
                action=action, stage=stage, target_sl=target_sl,
                mfe_r=mfe_r, giveback_r=giveback_r,
            ))
            session.commit()

    @staticmethod
    def _sl_close(a: "float | None", b: "float | None", tol: float = 1e-5) -> bool:
        """target_sl の一致判定。両方 None は一致 (共に SL なし)、片方のみ None は不一致、
        両方数値は tol 以内なら一致 (price_monitor と tick_worker は別 current 由来で
        bit 完全一致しないため浮動小数許容、Task 3 の pytest.approx と同方針)。"""
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= tol

    def compare_protection_decisions(
        self, *, since: datetime, max_delta_seconds: int = 60,
    ) -> list[dict]:
        """同 pair+order_id の price_monitor / tick_worker 判定を近接 ts でペアリングし
        一致を返す (review M-e)。

        各 price_monitor 行に対し、同 order_id・|ts差| <= max_delta_seconds の中で
        最も近い tick_worker 行を選ぶ。tick_worker は 2s 毎・price_monitor は数分毎で
        頻度が違うため、時間が離れた別局面を突き合わせて false match/mismatch を出さない。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(_ProtectionDecision)
                .where(_ProtectionDecision.ts >= since)
                .order_by(_ProtectionDecision.ts)
            ).scalars().all()

        pm_rows = [r for r in rows if r.source == "price_monitor"]
        tw_rows = [r for r in rows if r.source == "tick_worker"]

        out: list[dict] = []
        for pm in pm_rows:
            candidates = [
                tw for tw in tw_rows
                if tw.order_id == pm.order_id
                and abs((tw.ts - pm.ts).total_seconds()) <= max_delta_seconds
            ]
            if not candidates:
                continue
            tw = min(candidates, key=lambda x: abs((x.ts - pm.ts).total_seconds()))
            out.append({
                "order_id": pm.order_id,
                "pair": pm.pair,
                "action_match": pm.action == tw.action,
                "target_sl_match": self._sl_close(pm.target_sl, tw.target_sl),
                "price_monitor_action": pm.action,
                "tick_worker_action": tw.action,
                "delta_seconds": abs((tw.ts - pm.ts).total_seconds()),
            })
        return out
