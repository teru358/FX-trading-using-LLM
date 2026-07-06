"""Orchestrator Layer2 の strict output schema (dataclass + 手動 JSON parse) テスト。

design §5.4: PlannerOpportunity / ExecutionPlanDraft / PlannerFinalDecision /
EntryCondition / InvalidationCondition。LLM raw text を直接 plan にせず、
許可 vocabulary / Literal を __post_init__ で検証し、from_llm_json() で parse する。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.orchestrator.schemas import (
    EntryCondition,
    ExecutionPlanDraft,
    InvalidationCondition,
    PlannerFinalDecision,
    PlannerOpportunity,
    SchemaParseError,
)


# ---------------------------------------------------------------------------
# EntryCondition
# ---------------------------------------------------------------------------


class TestEntryCondition:
    def test_price_condition_valid(self) -> None:
        cond = EntryCondition(type="price_at_or_below", value=150.0)
        assert cond.type == "price_at_or_below"
        assert cond.value == 150.0

    def test_all_price_types_allowed(self) -> None:
        for t in ("price_at_or_below", "price_at_or_above", "breakout_above", "breakout_below"):
            EntryCondition(type=t, value=1.0)

    def test_spread_below_uses_value_pips(self) -> None:
        cond = EntryCondition(type="spread_below", value_pips=1.5)
        assert cond.value_pips == 1.5

    def test_technical_status_is_uses_status(self) -> None:
        cond = EntryCondition(type="technical_status_is", status="ok")
        assert cond.status == "ok"

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            EntryCondition(type="price_equals", value=1.0)

    def test_price_type_requires_value(self) -> None:
        with pytest.raises(ValueError):
            EntryCondition(type="price_at_or_below")

    def test_spread_below_requires_value_pips(self) -> None:
        with pytest.raises(ValueError):
            EntryCondition(type="spread_below")

    def test_technical_status_only_ok(self) -> None:
        with pytest.raises(ValueError):
            EntryCondition(type="technical_status_is", status="stale")

    def test_from_dict(self) -> None:
        cond = EntryCondition.from_dict({"type": "price_at_or_above", "value": 151.2})
        assert cond.type == "price_at_or_above"
        assert cond.value == 151.2

    def test_from_dict_coerces_string_value_to_float(self) -> None:
        # LLM が "150.0" のように文字列で数値を返しても float に正規化する
        cond = EntryCondition.from_dict({"type": "price_at_or_below", "value": "150.0"})
        assert cond.value == 150.0
        assert isinstance(cond.value, float)

    def test_from_dict_non_numeric_value_raises_schema_parse_error(self) -> None:
        with pytest.raises(SchemaParseError):
            EntryCondition.from_dict({"type": "price_at_or_below", "value": "abc"})


# ---------------------------------------------------------------------------
# InvalidationCondition
# ---------------------------------------------------------------------------


class TestInvalidationCondition:
    def test_price_below_valid(self) -> None:
        cond = InvalidationCondition(type="price_below", value=149.0)
        assert cond.value == 149.0

    def test_marker_types_need_no_value(self) -> None:
        for t in ("technical_stale", "news_conflict", "expired"):
            InvalidationCondition(type=t)

    def test_price_type_requires_value(self) -> None:
        with pytest.raises(ValueError):
            InvalidationCondition(type="price_below")

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            InvalidationCondition(type="rsi_overbought")

    def test_from_dict_coerces_string_value_to_float(self) -> None:
        cond = InvalidationCondition.from_dict({"type": "price_below", "value": "149.5"})
        assert cond.value == 149.5
        assert isinstance(cond.value, float)


# ---------------------------------------------------------------------------
# PlannerOpportunity
# ---------------------------------------------------------------------------


class TestPlannerOpportunity:
    def test_valid_yes(self) -> None:
        opp = PlannerOpportunity(
            opportunity="yes",
            direction="long",
            score=0.7,
            confidence=0.6,
            reasoning_summary="breakout setup",
        )
        assert opp.opportunity == "yes"
        assert opp.missing_inputs == []

    def test_opportunity_literal_enforced(self) -> None:
        with pytest.raises(ValueError):
            PlannerOpportunity(
                opportunity="maybe",
                direction="long",
                score=0.1,
                confidence=0.1,
                reasoning_summary="x",
            )

    def test_direction_literal_enforced(self) -> None:
        with pytest.raises(ValueError):
            PlannerOpportunity(
                opportunity="no",
                direction="sideways",
                score=0.1,
                confidence=0.1,
                reasoning_summary="x",
            )

    def test_from_llm_json_plain(self) -> None:
        raw = (
            '{"opportunity": "yes", "direction": "short", "score": 0.55, '
            '"confidence": 0.5, "reasoning_summary": "resistance reject", '
            '"missing_inputs": []}'
        )
        opp = PlannerOpportunity.from_llm_json(raw)
        assert opp.direction == "short"
        assert opp.score == 0.55

    def test_from_llm_json_strips_markdown_fences(self) -> None:
        raw = (
            "```json\n"
            '{"opportunity": "no", "direction": "none", "score": 0.0, '
            '"confidence": 0.2, "reasoning_summary": "no edge"}\n'
            "```"
        )
        opp = PlannerOpportunity.from_llm_json(raw)
        assert opp.opportunity == "no"
        assert opp.direction == "none"

    def test_from_llm_json_ignores_extra_keys(self) -> None:
        raw = (
            '{"opportunity": "yes", "direction": "long", "score": 0.6, '
            '"confidence": 0.6, "reasoning_summary": "ok", "extra_field": 42}'
        )
        opp = PlannerOpportunity.from_llm_json(raw)
        assert opp.opportunity == "yes"

    def test_from_llm_json_malformed_raises_schema_parse_error(self) -> None:
        with pytest.raises(SchemaParseError):
            PlannerOpportunity.from_llm_json("not json at all")

    def test_from_llm_json_missing_required_raises_schema_parse_error(self) -> None:
        with pytest.raises(SchemaParseError):
            PlannerOpportunity.from_llm_json('{"opportunity": "yes"}')

    def test_from_llm_json_strips_double_fences(self) -> None:
        raw = (
            "```json\n"
            "```json\n"
            '{"opportunity": "no", "direction": "none", "score": 0.0, '
            '"confidence": 0.1, "reasoning_summary": "x"}\n'
            "```\n"
            "```"
        )
        opp = PlannerOpportunity.from_llm_json(raw)
        assert opp.opportunity == "no"

    def test_from_llm_json_strips_fence_with_trailing_note(self) -> None:
        raw = (
            "```json\n"
            '{"opportunity": "yes", "direction": "long", "score": 0.5, '
            '"confidence": 0.5, "reasoning_summary": "x"}\n'
            "```\n"
            "Note: my reasoning above."
        )
        opp = PlannerOpportunity.from_llm_json(raw)
        assert opp.opportunity == "yes"

    def test_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlannerOpportunity(
                opportunity="yes",
                direction="long",
                score=1.5,
                confidence=0.5,
                reasoning_summary="x",
            )

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlannerOpportunity(
                opportunity="yes",
                direction="long",
                score=0.5,
                confidence=-0.1,
                reasoning_summary="x",
            )


# ---------------------------------------------------------------------------
# ExecutionPlanDraft
# ---------------------------------------------------------------------------


class TestExecutionPlanDraft:
    def _valid_kwargs(self) -> dict:
        return dict(
            direction="long",
            entry_conditions=[EntryCondition(type="price_at_or_below", value=150.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.5)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="pullback long",
        )

    def test_valid(self) -> None:
        draft = ExecutionPlanDraft(**self._valid_kwargs())
        assert draft.direction == "long"
        assert len(draft.entry_conditions) == 1

    def test_direction_literal_enforced(self) -> None:
        kw = self._valid_kwargs()
        kw["direction"] = "none"
        with pytest.raises(ValueError):
            ExecutionPlanDraft(**kw)

    def test_empty_entry_conditions_rejected(self) -> None:
        kw = self._valid_kwargs()
        kw["entry_conditions"] = []
        with pytest.raises(ValueError):
            ExecutionPlanDraft(**kw)

    def test_from_llm_json_builds_nested_conditions(self) -> None:
        raw = (
            "```json\n"
            "{"
            '"direction": "short",'
            '"entry_conditions": [{"type": "price_at_or_above", "value": 151.0}],'
            '"action": {"sl": 152.0, "tp": 149.0, "size_policy": "risk", "rr": 2.0, "comment": "fade"},'
            '"invalidation": [{"type": "price_above", "value": 152.5}, {"type": "expired"}],'
            '"expires_at": "2026-06-21T18:00:00+00:00",'
            '"reasoning_summary": "fade the spike"'
            "}\n"
            "```"
        )
        draft = ExecutionPlanDraft.from_llm_json(raw)
        assert draft.direction == "short"
        assert isinstance(draft.entry_conditions[0], EntryCondition)
        assert draft.entry_conditions[0].type == "price_at_or_above"
        assert isinstance(draft.invalidation[0], InvalidationCondition)
        assert draft.invalidation[1].type == "expired"
        assert draft.expires_at == datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc)

    def test_from_llm_json_bad_condition_raises_schema_parse_error(self) -> None:
        raw = (
            "{"
            '"direction": "long",'
            '"entry_conditions": [{"type": "bogus", "value": 1.0}],'
            '"action": {},'
            '"invalidation": [],'
            '"expires_at": "2026-06-21T18:00:00+00:00",'
            '"reasoning_summary": "x"'
            "}"
        )
        with pytest.raises(SchemaParseError):
            ExecutionPlanDraft.from_llm_json(raw)

    def test_to_storage_dict_serializes_conditions(self) -> None:
        draft = ExecutionPlanDraft(**self._valid_kwargs())
        payload = draft.to_storage_dict()
        assert payload["entry_conditions"] == [
            {"type": "price_at_or_below", "value": 150.0}
        ]
        assert payload["invalidation"] == [{"type": "price_below", "value": 148.5}]
        assert payload["direction"] == "long"

    def test_draft_scale_in_fields_parsed(self) -> None:
        raw = json.dumps({
            "direction": "long",
            "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
            "action": {"sl": 149.0, "tp": 152.0},
            "invalidation": [],
            "expires_at": "2026-07-05T20:00:00",
            "reasoning_summary": "test",
            "scale_in": True,
            "new_signal_evidence": "1h RSI divergence formed after original entry",
        })
        draft = ExecutionPlanDraft.from_llm_json(raw)
        assert draft.scale_in is True
        assert "divergence" in draft.new_signal_evidence
        d = draft.to_storage_dict()
        assert d["scale_in"] is True
        assert d["new_signal_evidence"]

    def test_draft_scale_in_defaults_false_when_absent(self) -> None:
        raw = json.dumps({
            "direction": "long",
            "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
            "action": {}, "invalidation": [],
            "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
        })
        draft = ExecutionPlanDraft.from_llm_json(raw)
        assert draft.scale_in is False
        assert draft.new_signal_evidence is None

    def test_draft_scale_in_true_without_evidence_parses(self) -> None:
        """scale_in=true + evidence 空でも parse は通る (whitespace は None に正規化)。

        evidence 必須は pipeline の決定的 gate が一元処理する: schema で raise すると
        run 全体が fail-safe failed になり feedback 再起案経路に乗れない (codex Medium)。
        """
        raw = json.dumps({
            "direction": "long",
            "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
            "action": {}, "invalidation": [],
            "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
            "scale_in": True, "new_signal_evidence": "  ",
        })
        draft = ExecutionPlanDraft.from_llm_json(raw)
        assert draft.scale_in is True
        assert draft.new_signal_evidence is None

    def test_draft_scale_in_rejects_non_bool(self) -> None:
        """codex Medium: bool("false") is True の丸め込みを許さない。型は JSON bool のみ。"""
        base = {
            "direction": "long",
            "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
            "action": {}, "invalidation": [],
            "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
        }
        with pytest.raises(SchemaParseError):
            ExecutionPlanDraft.from_llm_json(json.dumps({**base, "scale_in": "false"}))
        with pytest.raises(SchemaParseError):
            ExecutionPlanDraft.from_llm_json(
                json.dumps({**base, "scale_in": True, "new_signal_evidence": 123}))


# ---------------------------------------------------------------------------
# PlannerFinalDecision
# ---------------------------------------------------------------------------


class TestPlannerFinalDecision:
    def test_valid_accept(self) -> None:
        dec = PlannerFinalDecision(
            decision="accept",
            final_score=0.7,
            confidence=0.6,
            reasoning_summary="aligned",
        )
        assert dec.decision == "accept"
        assert dec.revision_request is None

    def test_decision_literal_enforced(self) -> None:
        with pytest.raises(ValueError):
            PlannerFinalDecision(decision="hold", reasoning_summary="x")

    def test_optional_fields_default_none(self) -> None:
        dec = PlannerFinalDecision(decision="reject", reasoning_summary="no edge")
        assert dec.final_score is None
        assert dec.confidence is None

    def test_revise_without_revision_request_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlannerFinalDecision(decision="revise", reasoning_summary="fix sl")

    def test_final_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlannerFinalDecision(
                decision="accept", reasoning_summary="x", final_score=2.0
            )

    def test_from_llm_json_revise_with_request(self) -> None:
        raw = (
            '{"decision": "revise", "reasoning_summary": "tighten sl", '
            '"revision_request": {"sl": 149.5}}'
        )
        dec = PlannerFinalDecision.from_llm_json(raw)
        assert dec.decision == "revise"
        assert dec.revision_request == {"sl": 149.5}

    def test_from_llm_json_malformed_raises_schema_parse_error(self) -> None:
        with pytest.raises(SchemaParseError):
            PlannerFinalDecision.from_llm_json("```\noops\n```")
