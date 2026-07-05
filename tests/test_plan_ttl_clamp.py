"""plan TTL クランプ (spec S-1 / codex Med#1): aware→naive 正規化 + 上限切り詰め。"""
from datetime import datetime, timedelta, timezone

from src.orchestrator.planning_pipeline import clamp_draft_ttl
from src.orchestrator.schemas import ExecutionPlanDraft, EntryCondition, InvalidationCondition
from src.utils.clock import db_now


def _draft(expires_at):
    return ExecutionPlanDraft(
        direction="long",
        entry_conditions=[EntryCondition.from_dict(
            {"type": "price_at_or_below", "value": 150.0})],
        action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": ""},
        invalidation=[InvalidationCondition.from_dict(
            {"type": "price_below", "value": 148.0})],
        expires_at=expires_at,
        reasoning_summary="test",
    )


def test_clamp_disabled_by_default_keeps_naive_expiry():
    exp = db_now() + timedelta(days=30)
    out = clamp_draft_ttl(_draft(exp), max_hours=0)
    assert out.expires_at == exp  # max_hours=0 = クランプ無効 (挙動不変)


def test_aware_expiry_is_normalized_to_naive_local():
    aware = datetime.now(timezone.utc) + timedelta(hours=2)
    out = clamp_draft_ttl(_draft(aware), max_hours=0)
    assert out.expires_at.tzinfo is None  # naive local (DB 規約)


def test_over_limit_is_clamped():
    exp = db_now() + timedelta(hours=48)
    out = clamp_draft_ttl(_draft(exp), max_hours=8)
    assert out.expires_at <= db_now() + timedelta(hours=8, seconds=5)


def test_under_limit_unchanged():
    exp = db_now() + timedelta(hours=3)
    out = clamp_draft_ttl(_draft(exp), max_hours=8)
    assert out.expires_at == exp
