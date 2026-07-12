# tests/test_main_scheduling_wiring.py
import inspect

import main


def test_technical_guard_registered():
    src = inspect.getsource(main)
    assert 'JobGuard("technical"' in src


def test_technical_dispatch_uses_guard_not_slot():
    src = inspect.getsource(main)
    # cadence tick / union dispatch が _run_with_guard 経由 (technical guard)
    assert "_run_with_slot, _cadence_tick" not in src
    assert "_run_with_slot, _tech_dispatch" not in src


def test_econ_impact_job_scheduled_with_slot():
    src = inspect.getsource(main)
    assert "run_econ_impact_collection" in src
