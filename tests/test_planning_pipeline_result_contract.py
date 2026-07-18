from src.orchestrator.planning_pipeline import PipelineResult, _normalize_reason


def test_normalize_reason_strips_newlines_and_truncates():
    raw = "line1\nline2\r\n" + "x" * 500
    out = _normalize_reason(raw)
    assert "\n" not in out and "\r" not in out
    assert len(out) <= 200


def test_normalize_reason_handles_none():
    assert _normalize_reason(None) == ""


def test_normalize_reason_is_idempotent_on_clean_input():
    assert _normalize_reason("plan created") == "plan created"


def test_normalize_reason_empty_falls_back_at_call_site():
    """空 reason は _normalize_reason では "" になる (fallback は呼び出し側の責務)。"""
    assert _normalize_reason("") == ""
    assert _normalize_reason("   ") == ""


def test_pipeline_result_has_reason_and_derived_rr():
    r = PipelineResult(outcome="direct_hold", reason="no opportunity")
    assert r.reason == "no opportunity"
    assert r.derived_rr is None
