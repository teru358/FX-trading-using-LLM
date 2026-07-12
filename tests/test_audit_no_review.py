# tests/test_audit_no_review.py
import importlib
import inspect

from src.analysis import performance_audit as pa


def test_run_audit_has_no_review_param():
    sig = inspect.signature(pa.run_audit)
    assert "review" not in sig.parameters


def test_audit_modules_removed():
    for mod in ("src.analysis.audit_reviewer", "src.analysis.audit_lesson_generator"):
        try:
            importlib.import_module(mod)
            assert False, f"{mod} should be deleted"
        except ModuleNotFoundError:
            pass


def test_performance_audit_no_lesson_imports():
    src = inspect.getsource(pa)
    assert "generate_candidates" not in src
    assert "interactive_review" not in src
    assert "audit_lessons_path" not in src
