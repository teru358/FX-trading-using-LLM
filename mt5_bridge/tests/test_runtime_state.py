"""RuntimeState (2-tier halt) のユニットテスト。"""
from __future__ import annotations

from pathlib import Path

from runtime_state import RuntimeState


def _make_env(tmp_path: Path, dry_run: str = "false") -> Path:
    env = tmp_path / ".env"
    env.write_text(f"DRY_RUN={dry_run}\nMT5_LOGIN=12345\n")
    return env


def test_initial_state_no_hard_halt(tmp_path: Path):
    env = _make_env(tmp_path, "false")
    state = RuntimeState(
        initial_dry_run=False, env_file=env, hard_halt_flag=tmp_path / "halt.flag",
    )
    assert state.dry_run is False
    assert state.soft_halted is False
    assert state.accepts_new_orders is True


def test_initial_state_with_hard_halt_flag(tmp_path: Path):
    env = _make_env(tmp_path, "true")
    flag = tmp_path / "halt.flag"
    flag.write_text("manual halt")
    state = RuntimeState(
        initial_dry_run=True, env_file=env, hard_halt_flag=flag,
    )
    assert state.dry_run is True
    assert state.is_hard_halted is True


def test_soft_halt_blocks_new_orders(tmp_path: Path):
    env = _make_env(tmp_path, "false")
    state = RuntimeState(
        initial_dry_run=False, env_file=env, hard_halt_flag=tmp_path / "halt.flag",
    )
    state.soft_halt(reason="test")
    assert state.accepts_new_orders is False
    assert state.soft_halted is True
    assert state.dry_run is False    # hard ではない
    # .env は触らない
    assert "DRY_RUN=false" in env.read_text()


def test_hard_halt_persists_to_env_and_flag(tmp_path: Path):
    env = _make_env(tmp_path, "false")
    flag = tmp_path / "halt.flag"
    state = RuntimeState(
        initial_dry_run=False, env_file=env, hard_halt_flag=flag,
    )
    state.hard_halt(reason="critical incident")
    assert state.dry_run is True
    assert state.is_hard_halted is True
    assert "DRY_RUN=true" in env.read_text()
    assert flag.exists()
    assert "critical incident" in flag.read_text()


def test_resume_clears_soft_halt(tmp_path: Path):
    env = _make_env(tmp_path, "false")
    state = RuntimeState(
        initial_dry_run=False, env_file=env, hard_halt_flag=tmp_path / "halt.flag",
    )
    state.soft_halt(reason="x")
    ok, _msg = state.resume()
    assert ok is True
    assert state.soft_halted is False
    assert state.accepts_new_orders is True


def test_resume_fails_during_hard_halt(tmp_path: Path):
    env = _make_env(tmp_path, "false")
    flag = tmp_path / "halt.flag"
    state = RuntimeState(
        initial_dry_run=False, env_file=env, hard_halt_flag=flag,
    )
    state.hard_halt(reason="x")
    ok, msg = state.resume()
    assert ok is False
    assert "hard halt" in msg.lower()
    assert state.dry_run is True   # まだ DRY_RUN 状態
