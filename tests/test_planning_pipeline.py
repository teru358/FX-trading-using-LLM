"""planning_pipeline (Task 2.6-2.9) テスト。

design §5.2 pipeline (Step2-6) + §5.3 re-draft + fail-safe + §6.1 active plan policy。

- opportunity=no → direct_hold のみ・plan を作らない
- opportunity=yes → draft → accept → risk pass → active plan + plan_create decision
- final reject → reject decision・plan を作らない
- risk fixable reject → 1 回再起案 → 2 回目も reject なら reject 終了
- risk structural reject → 再起案しない・reject 終了
- accept 時 supersede_active_plans で旧 active plan を superseded (pair 単位最大1)
- parse error / circuit open → no plan + failed (PipelineResult.outcome=='failed')
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.config.schema import AgentLlm, OrchestratorConfig
from src.data.orchestrator_store import OrchestratorStore
from src.llm.claude_cli_client import ClaudeCliUsageLimitError
from src.llm.client import CircuitOpenError
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
from src.orchestrator.planner_agent import PlannerAgent
from src.orchestrator.planning_pipeline import PlanningPipeline
from src.orchestrator.risk_gate import RiskGateWorker


# ── fakes ─────────────────────────────────────────────────────


class _ScriptedLLM:
    """与えた応答列を順に返す mock。Exception を入れると raise する。"""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature=0.1) -> str:
        self.calls.append(messages)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OPP_YES = (
    '{"opportunity": "yes", "direction": "long", "score": 0.7, '
    '"confidence": 0.6, "reasoning_summary": "breakout"}'
)
OPP_NO = (
    '{"opportunity": "no", "direction": "none", "score": 0.1, '
    '"confidence": 0.2, "reasoning_summary": "no edge"}'
)
FINAL_ACCEPT = (
    '{"decision": "accept", "final_score": 0.72, "confidence": 0.65, '
    '"reasoning_summary": "ok"}'
)
FINAL_REJECT = '{"decision": "reject", "reasoning_summary": "weak"}'
FINAL_REVISE = (
    '{"decision": "revise", "reasoning_summary": "tighten sl", '
    '"revision_request": {"sl": 149.5}}'
)


def _draft_json(*, sl=149.0, tp=152.0, rr=2.0, scale_in=None, evidence=None) -> str:
    extra = ""
    if scale_in is not None:
        extra += f',"scale_in": {"true" if scale_in else "false"}'
    if evidence is not None:
        extra += f',"new_signal_evidence": "{evidence}"'
    return (
        "{"
        '"direction": "long",'
        '"entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],'
        f'"action": {{"sl": {sl}, "tp": {tp}, "size_policy": "risk", "rr": {rr}, "comment": "x"}},'
        '"invalidation": [{"type": "price_below", "value": 148.0}],'
        '"expires_at": "2026-12-31T18:00:00+00:00",'
        '"reasoning_summary": "pullback long"'
        f"{extra}"
        "}"
    )


def _ctx(store: OrchestratorStore, *, halt="none", mid=150.0) -> dict:
    """本物の snapshot_id を含む decision context を組む。"""
    sid = store.create_snapshot(
        pair="USDJPY=X",
        as_of_time=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        quote_json={"mid": mid},
    )
    return {
        "snapshot_id": sid,
        "pair": "USDJPY=X",
        "quote": {"bid": mid - 0.01, "ask": mid + 0.01, "mid": mid, "spread": 0.02},
        "technical": {"status": "ok", "direction": "long", "bias_score": 0.4},
        "news": {"sentiment_score": 0.3, "confidence": 0.6, "top_reasons": []},
        "risk_state": {
            "halt": halt, "bridge_health": "healthy",
            "market_open": True, "cooldown": False,
        },
        "policy": {"trade_horizon": "swing", "advice_memo": ""},
    }


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def _make_pipeline(
    store: OrchestratorStore, llm, config: OrchestratorConfig | None = None,
) -> PlanningPipeline:
    bundle = AgentLlm(client=llm, temperature=0.1)
    return PlanningPipeline(
        orch_store=store,
        planner=PlannerAgent(bundle),
        execution_agent=ExecutionOpinionAgent(bundle),
        risk_gate=RiskGateWorker(min_rr=1.5, spread_max_pips=2.0, pip_size=0.01),
        config=config or OrchestratorConfig(),
    )


def _decision_types(store: OrchestratorStore, result) -> list[str]:
    return [store.get_decision(did).decision_type for did in result.decision_ids]


# ── opportunity = no ──────────────────────────────────────────


async def test_opportunity_no_records_direct_hold_only(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_NO])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "direct_hold"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []
    assert _decision_types(store, result) == ["direct_hold"]


# ── position unavailable → 決定的 direct_hold (P-4) ───────────


async def test_position_unavailable_skips_llm_and_holds(store: OrchestratorStore) -> None:
    """position.status=unavailable → LLM 不呼び出しで direct_hold (P-4)。"""
    llm = _ScriptedLLM([])  # 応答なし: LLM が呼ばれたら pop で即死する
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = {"count": None, "items": [], "status": "unavailable"}
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "direct_hold"
    assert result.plan_id is None
    assert llm.calls == []  # scan_opportunity すら呼ばれていない
    assert store.get_active_plans("USDJPY=X") == []
    dec = store.get_decision(result.decision_ids[0])
    assert dec.decision_type == "direct_hold"
    assert "position unavailable" in dec.reasoning_summary


async def test_current_plan_unavailable_skips_llm_and_holds(
    store: OrchestratorStore,
) -> None:
    """current_plan.status=unavailable → LLM 不呼び出しで direct_hold (P-4 と同一 gate)。

    読めなかった既存 plan を planner が知らずに置き換えるのを決定的に防ぐ。
    """
    llm = _ScriptedLLM([])  # 応答なし: LLM が呼ばれたら pop で即死する
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["current_plan"] = {"status": "unavailable"}
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "direct_hold"
    assert result.plan_id is None
    assert llm.calls == []
    assert store.get_active_plans("USDJPY=X") == []
    dec = store.get_decision(result.decision_ids[0])
    assert dec.decision_type == "direct_hold"
    assert "current_plan unavailable" in dec.reasoning_summary


# ── opportunity = yes → accept → risk pass → active plan ───────


async def test_accept_creates_active_plan(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.plan_id is not None
    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1
    assert active[0].plan_id == result.plan_id
    assert "plan_create" in _decision_types(store, result)


async def test_accept_records_agent_outputs_and_opinion(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert len(store.get_agent_outputs(run_id)) >= 1
    assert store.get_execution_opinion(run_id) is not None


async def test_accept_records_vote_linked_to_decision(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    # plan_create decision に紐づく vote が記録される (§5.2 record 順序)
    plan_decision = next(
        store.get_decision(did)
        for did in result.decision_ids
        if store.get_decision(did).decision_type == "plan_create"
    )
    votes = store.get_votes(plan_decision.decision_id)
    assert len(votes) >= 1


# ── final revise → re-draft (CRITICAL-1 fix) ──────────────────


async def test_revise_redrafts_then_accepts(store: OrchestratorStore) -> None:
    # 1st: planner revise → 再起案 → 2nd: accept → risk pass → plan
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_REVISE, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    assert len(store.get_active_plans("USDJPY=X")) == 1


async def test_revise_twice_stops_without_plan(store: OrchestratorStore) -> None:
    # 2 回連続 revise → 再起案予算 1 回を使い切り reject 終了 (plan 作らない)
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_REVISE, _draft_json(), FINAL_REVISE])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "reject"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []


# ── bad opportunity direction → fail-safe (MEDIUM-1 fix) ───────


async def test_opportunity_yes_with_none_direction_fails_safe(store: OrchestratorStore) -> None:
    bad = (
        '{"opportunity": "yes", "direction": "none", "score": 0.7, '
        '"confidence": 0.6, "reasoning_summary": "x"}'
    )
    llm = _ScriptedLLM([bad])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"
    assert store.get_active_plans("USDJPY=X") == []


# ── generic store error escapes to failed, not crash (HIGH-2) ──


async def test_unexpected_store_error_yields_failed(store: OrchestratorStore, monkeypatch) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    # create_trade_plan の後段 record_decision で予期せぬ例外を注入
    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "record_decision", _boom)

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"  # クラッシュせず failed を返す
    # orphan 防止 (Codex High#2): decision 記録に失敗したら active plan は残らない
    assert store.get_active_plans("USDJPY=X") == []


# ── final reject ──────────────────────────────────────────────


async def test_final_reject_creates_no_plan(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_REJECT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "reject"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []
    assert _decision_types(store, result) == ["reject"]


# ── risk fixable reject → 1 回再起案 ──────────────────────────


async def test_fixable_reject_redrafts_once_then_accepts(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM(
        [OPP_YES, _draft_json(rr=0.8), FINAL_ACCEPT, _draft_json(rr=2.0), FINAL_ACCEPT]
    )
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    assert len(store.get_active_plans("USDJPY=X")) == 1


async def test_fixable_reject_twice_stops(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM(
        [OPP_YES, _draft_json(rr=0.8), FINAL_ACCEPT, _draft_json(rr=0.8), FINAL_ACCEPT]
    )
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "reject"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []


# ── risk structural reject → 再起案しない ─────────────────────


async def test_structural_reject_does_not_redraft(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store, halt="hard")
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "reject"
    assert result.redraft_count == 0
    assert len(llm.calls) == 3  # scan + draft + final のみ (再起案なし)


# ── supersede (active plan policy) ────────────────────────────


async def test_accept_supersedes_existing_active_plan(store: OrchestratorStore) -> None:
    old_sid = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 6, 20, tzinfo=timezone.utc)
    )
    old = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=old_sid, horizon="swing", direction="long",
        entry_conditions_json=[], action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc), created_by_run_id=1,
    )
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1  # pair 単位最大 1
    assert active[0].plan_id == result.plan_id
    assert old != result.plan_id


# ── scale_in 決定的導出 (P-2b) ────────────────────────────────


def _position(*directions: str) -> dict:
    return {
        "count": len(directions),
        "items": [{"direction": d} for d in directions],
        "status": "ok",
    }


async def test_scale_in_coerced_false_without_position(store: OrchestratorStore) -> None:
    """建玉なしなのに LLM が scale_in=true を申告 → 導出値 False で上書き。"""
    llm = _ScriptedLLM(
        [OPP_YES, _draft_json(scale_in=True, evidence="claimed breakout"), FINAL_ACCEPT]
    )
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = _position()  # 建玉ゼロ
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1
    assert active[0].scale_in is False
    assert active[0].new_signal_evidence is None
    # 申告値 (raw claim) は coercion 前に永続化される。不一致率メトリクス (spec §4:
    # agent_outputs.structured_payload.scale_in vs trade_plans.scale_in) の原資。
    draft_outputs = [
        o for o in store.get_agent_outputs(run_id)
        if o.output_type == "execution_draft"
    ]
    assert len(draft_outputs) == 1
    assert draft_outputs[0].structured_payload_json["scale_in"] is True


async def test_same_direction_position_forces_scale_in(store: OrchestratorStore) -> None:
    """同方向建玉あり: evidence なし draft は再起案され、2 回目で scale_in plan になる。"""
    llm = _ScriptedLLM(
        [
            OPP_YES,
            _draft_json(),  # scale_in 申告なし・evidence なし → 決定的再起案
            _draft_json(scale_in=True, evidence="fresh 1h breakout after entry"),
            FINAL_ACCEPT,
        ]
    )
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = _position("long")
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1
    assert active[0].scale_in is True
    assert active[0].new_signal_evidence == "fresh 1h breakout after entry"
    # 再起案プロンプトに scale-in feedback が流れていること (calls[2] = 2 回目 draft)
    redraft_user_prompt = llm.calls[2][-1]["content"]
    assert "scale-in" in redraft_user_prompt
    assert "new_signal_evidence" in redraft_user_prompt


async def test_scale_in_claimed_true_without_evidence_redrafts(
    store: OrchestratorStore,
) -> None:
    """LLM が scale_in=true を申告しつつ evidence を欠く → schema fail ではなく
    他の不備と同じ feedback 再起案経路に乗る (evidence 必須は pipeline gate に一元化)。"""
    llm = _ScriptedLLM(
        [
            OPP_YES,
            _draft_json(scale_in=True),  # 申告 true・evidence なし → 決定的再起案
            _draft_json(scale_in=True, evidence="fresh 1h breakout after entry"),
            FINAL_ACCEPT,
        ]
    )
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = _position("long")
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1
    assert active[0].scale_in is True
    assert active[0].new_signal_evidence == "fresh 1h breakout after entry"
    redraft_user_prompt = llm.calls[2][-1]["content"]
    assert "new_signal_evidence" in redraft_user_prompt


async def test_scale_in_rejected_when_evidence_never_provided(
    store: OrchestratorStore,
) -> None:
    """同方向建玉あり: 2 回とも evidence なし → 決定的 reject (plan を作らない)。"""
    llm = _ScriptedLLM([OPP_YES, _draft_json(), _draft_json()])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = _position("long")
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "reject"
    assert result.plan_id is None
    assert result.redraft_count == 1
    assert "new_signal_evidence" in result.reason
    assert store.get_active_plans("USDJPY=X") == []
    # 決定的 redraft/reject で終わっても draft ごとに opinion 監査痕跡が残る
    # (2 draft → agent_outputs 2 行 + execution_opinion 記録あり)。
    draft_outputs = [
        o for o in store.get_agent_outputs(run_id)
        if o.output_type == "execution_draft"
    ]
    assert len(draft_outputs) == 2
    assert store.get_execution_opinion(run_id) is not None


async def test_opposite_direction_position_is_not_scale_in(
    store: OrchestratorStore,
) -> None:
    """逆方向建玉のみ → scale_in ではない (evidence 不要で plan になる)。"""
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    ctx["position"] = _position("short")
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    assert result.redraft_count == 0
    active = store.get_active_plans("USDJPY=X")
    assert len(active) == 1
    assert active[0].scale_in is False


# ── fail-safe ─────────────────────────────────────────────────


async def test_parse_error_yields_failed_no_plan(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, "not json at all"])  # draft parse error
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []


async def test_circuit_open_yields_failed_no_plan(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([CircuitOpenError("open")])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"
    assert result.plan_id is None


async def test_usage_limit_is_failsafe_warning_not_unexpected_error(
    store: OrchestratorStore, caplog
) -> None:
    """LLM usage limit (claude-cli 429 等) は想定内 fail-safe として扱う。

    本番 (2026-06-26) で claude-cli session limit が ``except Exception`` に落ち、
    ERROR + スタックトレースを吐いていた。これは想定内の一時障害なので
    ``planning fail-safe`` WARNING に倒し、``planning unexpected error`` ERROR
    (= logger.exception, スタックトレース) を出さない。
    """
    import logging

    llm = _ScriptedLLM([ClaudeCliUsageLimitError("session limit hit")])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.planning_pipeline"):
        result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"
    assert result.plan_id is None
    # 想定外 ERROR (logger.exception) を出していないこと。
    assert not any(
        r.levelno >= logging.ERROR and "unexpected error" in r.getMessage()
        for r in caplog.records
    ), "usage limit should be fail-safe WARNING, not an unexpected-error ERROR"
    # 想定内 fail-safe WARNING を出していること。
    assert any(
        r.levelno == logging.WARNING and "fail-safe" in r.getMessage()
        for r in caplog.records
    )


# ── approval gate (F-6) ──────────────────────────────────────


async def test_gate_on_publishes_pending_approval(store: OrchestratorStore) -> None:
    """gate ON: create は requires_replan のまま、最後の publish だけ pending_approval。"""
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm, config=OrchestratorConfig(approval_gate=True))
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "plan_create"
    plan = store.get_trade_plan(result.plan_id)
    assert plan.status == "pending_approval"
    assert plan.gate_decision is None
    assert result.decision_ids


async def test_gate_off_publishes_active_unchanged(store: OrchestratorStore) -> None:
    """gate OFF (既定): 現行どおり active で publish — 挙動不変。"""
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert store.get_trade_plan(result.plan_id).status == "active"
