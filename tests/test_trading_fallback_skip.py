# tests/test_trading_fallback_skip.py
import inspect

from src.cycles import trading, _helpers


def test_trading_does_not_import_analyze_price_action():
    src = inspect.getsource(trading)
    assert "analyze_price_action" not in src


def test_build_rag_context_removed_from_helpers():
    assert not hasattr(_helpers, "_build_rag_context")


def test_trading_source_has_no_fallback_analysis():
    src = inspect.getsource(trading)
    # the fallback path called _build_rag_context / analyze_price_action; both gone
    assert "_build_rag_context" not in src
    # skip semantics: no-snapshot returns None (skip), not an LLM analysis
    assert "no stored snapshots" in src  # warning log retained for the skip path
