"""BridgeHealthGate のユニットテスト。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from src.trading.bridge_health_gate import BridgeHealthGate, ProbeResult


def _make_config(*, mode="paper", live_broker=None, has_mt5=False, has_oanda=False):
    cfg = MagicMock()
    cfg.mode = mode
    cfg.live_broker = live_broker
    cfg.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
        request_timeout_seconds=5.0,
    ) if has_mt5 else None
    cfg.providers.oanda = MagicMock() if has_oanda else None
    cfg.state_dir = Path("data/state")
    return cfg


class _Resp:
    """httpx.get モック用レスポンス。"""
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"mt5_connected": True}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def raise_for_status(self):
        if not self.is_success:
            raise httpx.HTTPStatusError("err", request=None, response=self)

    def json(self):
        return self._payload


def test_paper_mode_always_ok(tmp_path):
    cfg = _make_config(mode="paper")
    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    result = gate.probe(caller="tech", sync_balance=True)
    assert result.ok is True
    assert result.retried is False


def test_first_probe_success_no_retry(tmp_path, monkeypatch):
    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    sleep_calls = []
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())
    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda s: sleep_calls.append(s),
    )
    gate._sync_balance = lambda: None  # Task 6 で実装、ここではスタブ

    result = gate.probe(caller="tech", sync_balance=True)
    assert result.ok is True
    assert result.retried is False
    assert sleep_calls == []


def test_first_fail_retry_success(tmp_path, monkeypatch):
    """1回目失敗 → 60秒sleep → 2回目成功で ok=True 返る、halt なし。"""
    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    sleep_calls = []
    responses = iter([_Resp(status_code=503), _Resp(status_code=200)])
    monkeypatch.setattr("httpx.get", lambda *a, **kw: next(responses))

    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda s: sleep_calls.append(s),
    )
    gate._sync_balance = lambda: None

    result = gate.probe(caller="tech", sync_balance=True)
    assert result.ok is True
    assert result.retried is True
    assert sleep_calls == [60.0]


def test_retry_uses_configured_delay(tmp_path, monkeypatch):
    """retry_after_sec=30 を渡すと sleep_fn(30.0) で呼ばれる。"""
    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    sleep_calls = []
    responses = iter([_Resp(status_code=500), _Resp(status_code=200)])
    monkeypatch.setattr("httpx.get", lambda *a, **kw: next(responses))

    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        retry_after_sec=30.0, sleep_fn=lambda s: sleep_calls.append(s),
    )
    gate._sync_balance = lambda: None
    gate.probe(caller="trading", sync_balance=True)
    assert sleep_calls == [30.0]


def test_first_fail_retry_fail_triggers_halt(tmp_path, monkeypatch):
    """1回目+2回目失敗で halt_state.trigger_auto + Discord 通知発火。"""
    from src.persistence import halt_state

    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    cfg.state_dir = tmp_path
    sleep_calls = []
    responses = iter([_Resp(status_code=503), _Resp(status_code=503)])
    monkeypatch.setattr("httpx.get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(
        "src.trading.bridge_health_gate.httpx.post",
        lambda *a, **kw: None,
    )

    notifier = MagicMock()

    gate = BridgeHealthGate(
        config=cfg, notifier=notifier,
        log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda s: sleep_calls.append(s),
    )
    gate._sync_balance = lambda: None

    result = gate.probe(caller="trading", sync_balance=True)
    assert result.ok is False
    assert result.retried is True
    state = halt_state.read(tmp_path)
    assert state.soft_halted is True
    assert state.triggered_by == "bridge_health_gate"


def test_double_call_no_double_halt(tmp_path, monkeypatch):
    """2回 probe を呼んでも halt は 1 回のみ (changed=False で多重防止)。"""
    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    cfg.state_dir = tmp_path
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp(status_code=503))
    monkeypatch.setattr(
        "src.trading.bridge_health_gate.httpx.post",
        lambda *a, **kw: None,
    )
    notifier = MagicMock()
    notifier.send_embed = MagicMock()

    # asyncio.run mock (notifier coroutine 消費)
    async def _consume(coro):
        try:
            coro.send(None)
        except (StopIteration, AttributeError):
            pass
    monkeypatch.setattr(
        "src.trading.bridge_health_gate.asyncio.run",
        lambda coro: _consume(coro),
    )

    gate = BridgeHealthGate(
        config=cfg, notifier=notifier,
        log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    gate._sync_balance = lambda: None

    gate.probe(caller="trading", sync_balance=True)
    gate.probe(caller="trading", sync_balance=True)
    # Discord 通知は 1 回のみ
    assert notifier.send_embed.call_count == 1


def test_balance_sync_only_in_live_mode(tmp_path, monkeypatch):
    """live_test では sync_balance=True でも balance.json を更新しない。"""
    cfg = _make_config(mode="live_test", live_broker="mt5", has_mt5=True)
    cfg.state_dir = tmp_path
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _Resp())

    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    sync_calls = []
    gate._sync_balance = lambda: sync_calls.append(1)
    gate.probe(caller="tech", sync_balance=True)
    assert sync_calls == []  # live_test では呼ばれない


def test_balance_sync_called_on_first_success(tmp_path, monkeypatch):
    """live + sync_balance=True で /account が叩かれ balance.json 更新される。"""
    from datetime import datetime, timezone
    from src.persistence import balance_snapshot

    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    cfg.state_dir = tmp_path
    balance_snapshot.write(tmp_path, balance_snapshot.BalanceSnapshot(
        balance=10000.0, deposit=10000.0, peak_balance=10000.0,
        source="paper",
        fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    ))

    def _fake_get(url, **kw):
        if url.endswith("/health"):
            return _Resp(payload={"mt5_connected": True})
        if url.endswith("/account"):
            return _Resp(payload={"balance": 50000.0})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("httpx.get", _fake_get)

    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    gate.probe(caller="tech", sync_balance=True)
    snap = balance_snapshot.read(tmp_path)
    assert snap.source == "mt5"
    assert snap.balance == 50000.0


def test_balance_sync_failure_does_not_halt(tmp_path, monkeypatch):
    """/account が失敗しても halt は発動しない (health は成功なので)。"""
    from src.persistence import halt_state

    cfg = _make_config(mode="live", live_broker="mt5", has_mt5=True)
    cfg.state_dir = tmp_path

    def _fake_get(url, **kw):
        if url.endswith("/health"):
            return _Resp(payload={"mt5_connected": True})
        return _Resp(status_code=500)

    monkeypatch.setattr("httpx.get", _fake_get)

    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    result = gate.probe(caller="tech", sync_balance=True)
    assert result.ok is True
    assert halt_state.read(tmp_path).soft_halted is False


def test_oanda_broker_enabled_returns_ok_placeholder(tmp_path):
    """live_broker=oanda で _enabled=True、probe は placeholder ok=True。"""
    cfg = _make_config(mode="live", live_broker="oanda", has_oanda=True)
    cfg.state_dir = tmp_path
    gate = BridgeHealthGate(
        config=cfg, log_path=tmp_path / "bridge_health.jsonl",
        sleep_fn=lambda _: None,
    )
    assert gate._enabled is True
    result = gate.probe(caller="tech", sync_balance=False)
    assert result.ok is True
