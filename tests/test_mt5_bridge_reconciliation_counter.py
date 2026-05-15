"""Mt5BridgeBrokerAdapter が reconciliation_state を更新することのテスト。

check_and_close_positions の skip / success 経路で
src.persistence.reconciliation_state を呼び、state_dir/reconciliation.json
の永続カウンタが正しく動くことを確認する。
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from src.persistence import reconciliation_state
from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter


def _make_broker(state_dir: Path) -> Mt5BridgeBrokerAdapter:
    """テスト用 minimal adapter。"""
    return Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812",
        api_key="",
        request_timeout_seconds=5.0,
        lot_size_units=100_000,
        magic_number=12345,
        state_dir=state_dir,
    )


def test_skip_increments_persisted_counter(tmp_path: Path, monkeypatch):
    """bridge 不通による reconciliation skip で永続カウンタが +1 される。"""
    broker = _make_broker(tmp_path)

    def _raise(*a, **kw):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr(httpx, "get", _raise)

    broker.check_and_close_positions(
        open_positions=[], current_prices={}, position_mgr=MagicMock(),
    )
    s1 = reconciliation_state.read(tmp_path)
    assert s1.skipped_consecutive == 1

    broker.check_and_close_positions([], {}, MagicMock())
    s2 = reconciliation_state.read(tmp_path)
    assert s2.skipped_consecutive == 2


def test_skip_counter_survives_new_broker_instance(tmp_path: Path, monkeypatch):
    """broker インスタンスを作り直してもカウンタが永続して引き継がれる。"""
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            httpx.ConnectError("down")))

    broker1 = _make_broker(tmp_path)
    broker1.check_and_close_positions([], {}, MagicMock())
    broker1.check_and_close_positions([], {}, MagicMock())

    # exit_check_cycle 相当: 新しい broker インスタンス生成
    broker2 = _make_broker(tmp_path)
    broker2.check_and_close_positions([], {}, MagicMock())

    s = reconciliation_state.read(tmp_path)
    assert s.skipped_consecutive == 3, (
        "新 broker インスタンスでもカウンタが引き継がれる必要がある"
    )


def test_successful_reconciliation_resets_counter(tmp_path: Path, monkeypatch):
    """bridge 復活で reconciliation 成功 → カウンタ 0 リセット。"""
    # skip 2 回
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            httpx.ConnectError("down")))
    broker = _make_broker(tmp_path)
    broker.check_and_close_positions([], {}, MagicMock())
    broker.check_and_close_positions([], {}, MagicMock())
    assert reconciliation_state.read(tmp_path).skipped_consecutive == 2

    # bridge 復活
    class _Resp:
        is_success = True
        def json(self): return []
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    broker.check_and_close_positions([], {}, MagicMock())

    s = reconciliation_state.read(tmp_path)
    assert s.skipped_consecutive == 0
    assert s.stale_warned is False
    assert s.last_recovered_at is not None


def test_threshold_warning_logged_once(tmp_path: Path, monkeypatch, caplog):
    """連続 6 回 skip で warning が 1 回だけ出る (それ以降は抑制)。"""
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            httpx.ConnectError("down")))
    caplog.set_level(logging.WARNING, logger="src.trading.mt5_bridge_broker")

    broker = _make_broker(tmp_path)

    # 5 回までは stale 警告は出ない
    for _ in range(5):
        broker.check_and_close_positions([], {}, MagicMock())
    stale_before = [r for r in caplog.records if "reconciliation stale" in r.message]
    assert len(stale_before) == 0

    # 6 回目で stale 警告
    broker.check_and_close_positions([], {}, MagicMock())
    stale_at = [r for r in caplog.records if "reconciliation stale" in r.message]
    assert len(stale_at) == 1
    assert "6 consecutive" in stale_at[0].message

    # さらに 5 回でも追加警告なし (永続化された stale_warned で抑制)
    for _ in range(5):
        broker.check_and_close_positions([], {}, MagicMock())
    stale_after = [r for r in caplog.records if "reconciliation stale" in r.message]
    assert len(stale_after) == 1
