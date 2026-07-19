"""fail-fast 起動 (spec §1.5 / plan Task 7) のテスト。

main() を直接叩くのは重いので、判定 (ensure_orchestrator_or_exit) と
起動順序 (run_startup_sequence) をそれぞれ関数 seam に切り出して検証する。
"""
from __future__ import annotations

import pytest

from src.startup import ensure_orchestrator_or_exit, run_startup_sequence


class TestEnsureOrExit:
    def test_orchestrator_none_raises_system_exit(self):
        with pytest.raises(SystemExit) as exc:
            ensure_orchestrator_or_exit(None, enabled=True)
        # 終了コード 1 (supervisor の Restart=on-failure を発火させるため)
        assert exc.value.code == 1

    def test_disabled_raises_system_exit(self):
        with pytest.raises(SystemExit) as exc:
            ensure_orchestrator_or_exit(None, enabled=False)
        assert exc.value.code == 1

    def test_disabled_raises_even_with_runtime(self):
        # enabled=false は runtime の有無に関わらず起動中止 (spec §1.5)。
        with pytest.raises(SystemExit) as exc:
            ensure_orchestrator_or_exit(object(), enabled=False)
        assert exc.value.code == 1

    def test_live_mode_passes_without_warning(self, caplog):
        with caplog.at_level("WARNING"):
            ensure_orchestrator_or_exit(object(), enabled=True, mode="live")
        assert not any("発注は行われません" in r.message for r in caplog.records)

    def test_shadow_mode_warns_not_exits(self, caplog):
        with caplog.at_level("WARNING"):
            ensure_orchestrator_or_exit(object(), enabled=True, mode="shadow")
        assert any("発注は行われません" in r.message for r in caplog.records)

    def test_observe_mode_warns_not_exits(self, caplog):
        # observe は schema の有効値 (src/config/schema.py)。live 以外は一律 warning
        # で通す — mode == "shadow" のような値決め打ちにしない。
        with caplog.at_level("WARNING"):
            ensure_orchestrator_or_exit(object(), enabled=True, mode="observe")
        assert any("発注は行われません" in r.getMessage() for r in caplog.records)

    def test_fatal_reason_goes_to_logger(self, caplog):
        # console 出力とログファイルは別経路 (logging_setup に StreamHandler が無い)。
        # 中止理由が /logs にも残るよう logger にも出す。
        with caplog.at_level("CRITICAL"), pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=False)
        assert any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_fatal_build_failure_goes_to_logger(self, caplog):
        with caplog.at_level("CRITICAL"), pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=True)
        assert any(r.levelname == "CRITICAL" for r in caplog.records)


class TestStartupSequence:
    """build → validate → API → initialize → start → scheduler の順序保証。

    - orchestrator の構築 + 起動可能性検証は全ての起動より前 (spec §1.5)。
    - 制御面 (halt/resume・承認ゲート・/status) を担う REST API は発注ループ
      (runtime.start()) より前に上げる。API が EADDRINUSE 等で落ちたとき、
      まだ発注ループが動いていない状態で中止できる (レビュー H-1)。
      初回収集 (LLM 込みで数分) の間 API が無応答になる問題も併せて解消する。
    - initialize (初回 news/tech 収集) は runtime.start() より前 (plan レビュー
      High-1: orchestrator の planning/watch loop は start() 直後から動くため、
      初回収集前の古い snapshot で判断・発注させない)。
    """

    def _spies(self):
        order = []
        runtime = type("R", (), {
            "start": lambda self: order.append("start"),
            "stop": lambda self: order.append("stop"),
        })()
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
        assert order == ["build", "validate", "api", "init", "start", "scheduler"]

    def test_returns_built_runtime(self):
        order, fns = self._spies()
        runtime = type("R", (), {
            "start": lambda self: order.append("start"),
            "stop": lambda self: order.append("stop"),
        })()
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        assert run_startup_sequence(**fns) is runtime

    def test_validate_receives_built_runtime(self):
        order, fns = self._spies()
        runtime = type("R", (), {
            "start": lambda self: order.append("start"),
            "stop": lambda self: order.append("stop"),
        })()
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
        with pytest.raises(SystemExit) as exc:
            run_startup_sequence(**fns)
        assert exc.value.code == 1
        assert order == ["build", "validate"]

    def test_api_starts_before_runtime_start(self):
        """制御面 (API) が発注ループより先に上がる (レビュー H-1)。"""
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order.index("api") < order.index("start")

    def test_initialize_runs_before_runtime_start(self):
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order.index("init") < order.index("start")

    def test_api_failure_stops_initialize_and_start(self):
        """API 起動失敗 (EADDRINUSE 等) では発注ループを一切起こさない。"""
        order, fns = self._spies()

        def api_boom():
            order.append("api")
            raise RuntimeError("api failed")

        fns["start_api"] = api_boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build", "validate", "api"]
        assert "start" not in order and "stop" not in order

    def test_initialize_failure_stops_start_and_scheduler(self):
        order, fns = self._spies()

        def boom():
            order.append("init")
            raise RuntimeError("initial collection failed")

        fns["initialize"] = boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build", "validate", "api", "init"]  # start/scheduler なし
        # start していないので stop も呼ばない
        assert "stop" not in order

    def test_start_failure_stops_scheduler_and_stops_runtime(self):
        order, fns = self._spies()

        def start_boom(self):
            order.append("start")
            raise RuntimeError("start failed")

        runtime = type("R", (), {
            "start": start_boom,
            "stop": lambda self: order.append("stop"),
        })()
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert "scheduler" not in order
        assert order[-1] == "stop"

    def test_scheduler_failure_stops_runtime(self):
        """部分起動 (runtime だけ動いている) 状態を残さない (レビュー H-1)。

        main.py の try/finally は run_startup_sequence より後ろにあるため、
        ここで stop() しないと通知 worker drain / quote producer 停止 /
        plan finalize が走らないまま例外が抜ける。
        """
        order, fns = self._spies()

        def sched_boom():
            order.append("scheduler")
            raise RuntimeError("scheduler failed")

        fns["start_scheduler"] = sched_boom
        with pytest.raises(RuntimeError, match="scheduler failed"):
            run_startup_sequence(**fns)
        assert order == ["build", "validate", "api", "init", "start", "scheduler", "stop"]

    def test_stop_failure_does_not_mask_original_exception(self):
        """stop() 自体が飛んでも元の失敗理由を握り潰さない。"""
        order, fns = self._spies()

        def start_boom(self):
            order.append("start")
            raise RuntimeError("original failure")

        def stop_boom(self):
            order.append("stop")
            raise RuntimeError("stop also failed")

        runtime = type("R", (), {"start": start_boom, "stop": stop_boom})()
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        with pytest.raises(RuntimeError, match="original failure"):
            run_startup_sequence(**fns)
        assert "stop" in order

    def test_keyboard_interrupt_during_start_stops_runtime(self):
        """BaseException (Ctrl+C 等) でも部分起動を残さない。"""
        order, fns = self._spies()

        def sched_boom():
            order.append("scheduler")
            raise KeyboardInterrupt()

        fns["start_scheduler"] = sched_boom
        with pytest.raises(KeyboardInterrupt):
            run_startup_sequence(**fns)
        assert order[-1] == "stop"
