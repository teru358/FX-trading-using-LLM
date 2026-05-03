"""Phase 3b で再編した /health, /status, /account の最小動作テスト。

フル統合は手動 (Task 17) で検証する。ここでは:
- /health が軽量フィールドのみを返す
- /status がシステム健全性キーを含む
- /account が残高・ポジション形式を返す
- 旧 /status のフォーマットは /account に移動済
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api._state import state
from src.api.server import app


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "test-key")


@pytest.fixture
def _state_setup(tmp_path, monkeypatch):
    """state.config / state.analysis_store を最小構成で注入する。"""
    cfg = MagicMock()
    cfg.trading.trading_mode = "paper"
    cfg.trading.initial_balance = 100_000.0
    cfg.state_dir = tmp_path
    cfg.mt5_bridge.bridge_url = ""
    cfg.tradeable_instruments = []
    cfg.price_provider.realtime_provider = "yfinance"
    cfg.price_provider.twelvedata.daily_limit = 800
    cfg.price_provider.twelvedata.watch_symbols = []
    cfg.price_provider.twelvedata.use_for_monitor = True
    cfg.price_provider.twelvedata.per_minute_limit = 8
    cfg.price_monitor.interval_minutes = 5
    cfg.schedule.run_times = []
    cfg.mt5_bridge.request_timeout_seconds = 5.0
    cfg.mt5_bridge.fallback.failure_window_sec = 300
    cfg.mt5_bridge.fallback.failure_threshold = 2
    cfg.mt5_bridge.fallback.heartbeat_interval_degraded_min = 15

    state.config = cfg
    state.analysis_store = None
    yield cfg
    state.config = None
    state.analysis_store = None


def _client_get(path: str, **kw):
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", **kw.pop("headers", {})}
    return client.get(path, headers=headers, **kw)


def test_health_lightweight(_state_setup):
    """/health は軽量化され、サブシステム状態は含まない。"""
    resp = _client_get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "trading_mode" in data
    assert "uptime_seconds" in data
    assert "scheduler" in data
    # 旧フィールドが残っていない (Phase 3b で /status に移動)
    assert "llm_circuit_breakers" not in data
    assert "snapshots" not in data


def test_status_returns_subsystem_health(_state_setup):
    """/status はシステム健全性キーを含む (新定義)。"""
    resp = _client_get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_circuit_breakers" in data
    assert "price_provider" in data
    assert "snapshots" in data
    assert "mt5_bridge" in data
    # MT5 bridge が未設定 → configured: False
    assert data["mt5_bridge"]["configured"] is False
    # 旧フィールドは含まない (/account に移動)
    assert "balance" not in data
    assert "open_positions" not in data


def test_account_returns_balance_and_positions(_state_setup):
    """/account は残高 + ポジション形式 (旧 /status の内容)。"""
    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    assert "balance" in data
    assert "initial_balance" in data
    assert "pnl" in data
    assert "pnl_pct" in data
    assert "total_trades" in data
    assert "win_rate" in data
    assert "open_positions" in data
    # 初期状態: 残高 = 初期残高、ポジションなし
    assert data["balance"] == 100_000.0
    assert data["open_positions"] == []


# ── /admin/halt /admin/resume プロキシ ──

def _client_post(path: str, **kw):
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", **kw.pop("headers", {})}
    return client.post(path, headers=headers, **kw)


def test_admin_halt_503_when_bridge_not_configured(_state_setup):
    """bridge_url 未設定で /admin/halt → 503。"""
    _state_setup.mt5_bridge.bridge_url = ""
    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    assert resp.status_code == 503


def test_admin_halt_proxies_to_bridge(_state_setup, monkeypatch):
    """bridge へ POST /admin/halt がプロキシされ、レスポンスがそのまま返る。"""
    _state_setup.mt5_bridge.bridge_url = "http://x:8812"

    captured: dict = {}

    class _R:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"dry_run": False, "soft_halted": True, "is_hard_halted": False,
                    "accepts_new_orders": False, "mt5_connected": True}

    def _post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _R()

    monkeypatch.setattr("httpx.post", _post)

    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["soft_halted"] is True
    assert captured["url"] == "http://x:8812/admin/halt"
    assert captured["json"]["mode"] == "soft"
    assert captured["json"]["reason"] == "test"


def test_admin_halt_502_when_bridge_unreachable(_state_setup, monkeypatch):
    """bridge HTTP error → 502。"""
    _state_setup.mt5_bridge.bridge_url = "http://x:8812"

    def _post(*a, **kw):
        raise httpx.ConnectError("down")

    import httpx
    monkeypatch.setattr("httpx.post", _post)
    resp = _client_post("/admin/halt", json={"mode": "soft"})
    assert resp.status_code == 502


def test_admin_resume_proxies_to_bridge(_state_setup, monkeypatch):
    _state_setup.mt5_bridge.bridge_url = "http://x:8812"

    class _R:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"dry_run": False, "soft_halted": False, "is_hard_halted": False,
                    "accepts_new_orders": True, "mt5_connected": True}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["soft_halted"] is False
    assert body["accepts_new_orders"] is True


def test_admin_resume_403_when_hard_halted(_state_setup, monkeypatch):
    """bridge が 403 を返したら finance も 403 を返す (hard halt 中の resume 拒否)。"""
    _state_setup.mt5_bridge.bridge_url = "http://x:8812"
    import httpx

    class _Req:
        url = "http://x:8812/admin/resume"

    class _R:
        status_code = 403
        text = "hard halt cannot be resumed remotely"
        def raise_for_status(self):
            raise httpx.HTTPStatusError("403", request=_Req(), response=self)
        def json(self):
            return {}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R())
    resp = _client_post("/admin/resume")
    assert resp.status_code == 403
