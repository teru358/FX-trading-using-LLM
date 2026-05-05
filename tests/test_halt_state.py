"""HaltState read/write/mutate のユニットテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.persistence.halt_state import (
    HaltState,
    mutate,
    read,
    write,
)


def test_read_returns_default_when_missing(tmp_path: Path):
    """halt.json 不在時は default (soft_halted=False) を返し、ファイルは作らない。"""
    state = read(tmp_path)
    assert state.soft_halted is False
    assert state.auto_triggered is False
    assert state.reason == ""
    assert state.since is None
    assert state.triggered_by == ""
    # halt.json は存在しない (balance.json と異なり bootstrap しない)
    assert not (tmp_path / "halt.json").exists()


def test_write_then_read_roundtrip(tmp_path: Path):
    """write → read で同一の HaltState を取得できる。"""
    state = HaltState(
        soft_halted=True,
        auto_triggered=True,
        reason="3 consecutive /health failures",
        since="2026-05-05T03:47:48+00:00",
        triggered_by="heartbeat",
    )
    write(tmp_path, state)
    assert read(tmp_path) == state
    # ディスクには JSON として書き込まれている
    p = tmp_path / "halt.json"
    assert json.loads(p.read_text())["soft_halted"] is True


def test_corrupted_json_returns_default(tmp_path: Path):
    """halt.json 破損時は default を返し、ログに ERROR を出す (上書きはしない)。"""
    p = tmp_path / "halt.json"
    p.write_text("{ invalid json", encoding="utf-8")
    state = read(tmp_path)
    assert state.soft_halted is False
