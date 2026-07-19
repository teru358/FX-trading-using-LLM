"""fail-fast 起動 (spec §1.5 / plan Task 7) のテスト。

main() を直接叩くのは重いので、判定 (ensure_orchestrator_or_exit) と
起動順序 (run_startup_sequence) をそれぞれ関数 seam に切り出して検証する。
"""
from __future__ import annotations

import pytest

from src.startup import ensure_orchestrator_or_exit, run_startup_sequence


class TestEnsureOrExit:
    def test_orchestrator_none_raises_system_exit(self):
        with pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=True)

    def test_disabled_raises_system_exit(self):
        with pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=False)

    def test_disabled_raises_even_with_runtime(self):
        # enabled=false は runtime の有無に関わらず起動中止 (spec §1.5)。
        with pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(object(), enabled=False)

    def test_live_mode_passes_without_warning(self, caplog):
        with caplog.at_level("WARNING"):
            ensure_orchestrator_or_exit(object(), enabled=True, mode="live")
        assert not any("発注は行われません" in r.message for r in caplog.records)

    def test_shadow_mode_warns_not_exits(self, caplog):
        with caplog.at_level("WARNING"):
            ensure_orchestrator_or_exit(object(), enabled=True, mode="shadow")
        assert any("発注は行われません" in r.message for r in caplog.records)


class TestStartupSequence:
    """build → validate → initialize → start → API → scheduler の順序保証。

    initialize (初回 news/tech 収集) は runtime.start() より前 (plan レビュー High-1:
    orchestrator の planning/watch loop は start() 直後から動くため、
    初回収集前の古い snapshot で判断・発注させない)。
    """

    def _spies(self):
        order = []
        runtime = type("R", (), {"start": lambda self: order.append("start")})()
        return order, {
            "build": lambda: (order.append("build"), runtime)[1],
            "validate": lambda rt: order.append("validate"),
            "initialize": lambda: order.append("init"),
            "start_api": lambda: order.append("api"),
            "start_scheduler": lambda: order.append("scheduler"),
        }

    def test_happy_path_order(self):
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order == ["build", "validate", "init", "start", "api", "scheduler"]

    def test_returns_built_runtime(self):
        order, fns = self._spies()
        built = []
        runtime = type("R", (), {"start": lambda self: order.append("start")})()
        fns["build"] = lambda: (order.append("build"), built.append(runtime), runtime)[2]
        assert run_startup_sequence(**fns) is runtime

    def test_validate_receives_built_runtime(self):
        order, fns = self._spies()
        runtime = type("R", (), {"start": lambda self: order.append("start")})()
        seen = []
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        fns["validate"] = lambda rt: (order.append("validate"), seen.append(rt))[0]
        run_startup_sequence(**fns)
        assert seen == [runtime]

    def test_build_failure_stops_everything_after(self):
        order, fns = self._spies()

        def boom():
            order.append("build")
            raise RuntimeError("bootstrap failed")

        fns["build"] = boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build"]

    def test_validate_exit_stops_everything_after(self):
        order, fns = self._spies()

        def refuse(rt):
            order.append("validate")
            raise SystemExit(1)

        fns["validate"] = refuse
        with pytest.raises(SystemExit):
            run_startup_sequence(**fns)
        assert order == ["build", "validate"]

    def test_initialize_runs_before_runtime_start(self):
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order.index("init") < order.index("start")

    def test_initialize_failure_stops_start_api_scheduler(self):
        order, fns = self._spies()

        def boom():
            order.append("init")
            raise RuntimeError("initial collection failed")

        fns["initialize"] = boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build", "validate", "init"]  # start/api/scheduler なし

    def test_start_failure_stops_before_api_and_scheduler(self):
        order, fns = self._spies()
        runtime = type("R", (), {"start": lambda self: (_ for _ in ()).throw(
            RuntimeError("start failed"))})()
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert "api" not in order and "scheduler" not in order

    def test_api_starts_before_scheduler(self):
        order, fns = self._spies()

        def api_boom():
            order.append("api")
            raise RuntimeError("api failed")

        fns["start_api"] = api_boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert "scheduler" not in order
