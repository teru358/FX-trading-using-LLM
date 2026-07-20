"""起動状態 (starting / ready / failed) の公開 (レビュー Medium)。

Task 7 で起動順序を API → 初回収集 → runtime → scheduler にしたため、API が応答を
返し始めた時点ではまだ初回 LLM 収集中・scheduler 未起動でありうる。/status が常に
`ok` を返すと外部監視から ready に見えてしまう。

契約:
  - API 起動直後は `starting`
  - scheduler 起動完了後にのみ `ready`
  - 観測可能な失敗 (プロセスが生き残る失敗) は `failed`
"""
from __future__ import annotations

import pytest

from src.api._state import state
from src.startup import run_startup_sequence


@pytest.fixture(autouse=True)
def _reset_state():
    prev = getattr(state, "readiness", None)
    yield
    state.readiness = prev


# ── APIState の readiness フィールド ────────────────────────────


def test_readiness_defaults_to_starting():
    """既定は starting — ready を名乗るのは scheduler 起動後だけ。"""
    from src.api._state import APIState

    assert APIState().readiness == "starting"


def test_status_reports_starting_before_scheduler(monkeypatch):
    """scheduler 未起動なら /status は starting を返す。"""
    from src.api.routes import health

    state.readiness = "starting"
    monkeypatch.setattr(health, "_get_mt5_bridge_status", lambda: {"configured": False})

    body = health.status()

    assert body["status"] == "starting"


def test_status_reports_ready_after_scheduler(monkeypatch):
    from src.api.routes import health

    state.readiness = "ready"
    monkeypatch.setattr(health, "_get_mt5_bridge_status", lambda: {"configured": False})

    body = health.status()

    assert body["status"] == "ready"


def test_status_reports_failed(monkeypatch):
    from src.api.routes import health

    state.readiness = "failed"
    monkeypatch.setattr(health, "_get_mt5_bridge_status", lambda: {"configured": False})

    body = health.status()

    assert body["status"] == "failed"


# ── run_startup_sequence が readiness を遷移させること ───────────


class _FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _seq(**over):
    rt = _FakeRuntime()
    kw = dict(
        build=lambda: rt,
        validate=lambda r: None,
        initialize=lambda: None,
        start_api=lambda: None,
        start_scheduler=lambda: None,
    )
    kw.update(over)
    return rt, kw


def test_readiness_is_starting_during_initialize():
    """初回収集の最中はまだ starting (ready を名乗らない)。"""
    observed = []
    rt, kw = _seq(initialize=lambda: observed.append(state.readiness))

    state.readiness = "starting"
    run_startup_sequence(**kw)

    assert observed == ["starting"]


def test_readiness_is_starting_until_scheduler_started():
    """runtime.start() 直後・scheduler 起動前も starting のまま。"""
    observed = []
    rt, kw = _seq(start_scheduler=lambda: observed.append(state.readiness))

    state.readiness = "starting"
    run_startup_sequence(**kw)

    assert observed == ["starting"]


def test_readiness_becomes_ready_after_full_sequence():
    rt, kw = _seq()

    state.readiness = "starting"
    run_startup_sequence(**kw)

    assert state.readiness == "ready"


def test_readiness_failed_when_scheduler_raises():
    """scheduler 起動失敗は観測可能 — runtime.stop() 後も API は生きている。"""
    def boom():
        raise RuntimeError("scheduler boom")

    rt, kw = _seq(start_scheduler=boom)

    state.readiness = "starting"
    with pytest.raises(RuntimeError):
        run_startup_sequence(**kw)

    assert state.readiness == "failed"
    assert rt.stopped, "失敗時は runtime を畳む既存契約が維持されること"


def test_readiness_failed_when_initialize_raises():
    """初回収集の失敗も API 起動後なので観測可能。"""
    def boom():
        raise RuntimeError("initialize boom")

    rt, kw = _seq(initialize=boom)

    state.readiness = "starting"
    with pytest.raises(RuntimeError):
        run_startup_sequence(**kw)

    assert state.readiness == "failed"


def test_readiness_failed_when_runtime_start_raises():
    class _Boom(_FakeRuntime):
        def start(self):
            raise RuntimeError("runtime boom")

    rt = _Boom()
    _, kw = _seq(build=lambda: rt)

    state.readiness = "starting"
    with pytest.raises(RuntimeError):
        run_startup_sequence(**kw)

    assert state.readiness == "failed"
