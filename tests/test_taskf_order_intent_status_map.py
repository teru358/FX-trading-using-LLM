import pytest

from src.trading.order_intent_status import (
    EXECUTION_OUTCOME_TO_INTENT_STATUS,
    intent_status_for_outcome,
    is_alertable_outcome,
)
from src.data.orchestrator_store import ORDER_INTENT_STATUSES


def test_mapping_covers_all_execution_outcomes():
    for outcome in ("executed", "skipped", "halted", "rejected", "failed"):
        assert outcome in EXECUTION_OUTCOME_TO_INTENT_STATUS


def test_mapped_statuses_are_valid_enum_values():
    for status in EXECUTION_OUTCOME_TO_INTENT_STATUS.values():
        assert status in ORDER_INTENT_STATUSES


def test_executed_maps_to_filled():
    assert intent_status_for_outcome("executed") == "filled"


def test_skipped_maps_to_abandoned():
    assert intent_status_for_outcome("skipped") == "abandoned"


def test_halted_and_rejected_map_to_rejected():
    assert intent_status_for_outcome("halted") == "rejected"
    assert intent_status_for_outcome("rejected") == "rejected"


def test_failed_maps_to_failed():
    assert intent_status_for_outcome("failed") == "failed"


def test_unknown_outcome_raises():
    with pytest.raises(KeyError):
        intent_status_for_outcome("bogus")


def test_alertable_outcomes():
    # executed / skipped は想定内 (alert なし)、halted/rejected/failed は要注意
    assert is_alertable_outcome("executed") is False
    assert is_alertable_outcome("skipped") is False
    assert is_alertable_outcome("halted") is True
    assert is_alertable_outcome("rejected") is True
    assert is_alertable_outcome("failed") is True
