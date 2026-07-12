# tests/test_cli_no_compare.py
import importlib
import inspect

from src import cli, tui


def test_cli_has_no_compare_command():
    src = inspect.getsource(cli)
    assert "compare_models" not in src
    assert "run_compare" not in src
    assert 'cmd == "compare"' not in src


def test_tui_has_no_compare_stub():
    src = inspect.getsource(tui)
    assert 'cmd == "compare"' not in src


def test_compare_models_module_gone():
    try:
        importlib.import_module("src.commands.compare_models")
        assert False, "compare_models should be deleted"
    except ModuleNotFoundError:
        pass
