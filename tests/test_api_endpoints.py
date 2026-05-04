"""Phase 3b で再編した /health, /status, /account の最小動作テスト。

フル統合は手動 (Task 17) で検証する。ここでは:
- /health が軽量フィールドのみを返す
- /status がシステム健全性キーを含む
- /account が残高・ポジション形式を返す
- 旧 /status のフォーマットは /account に移動済
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api._state import state
from src.api.server import app
from src.persistence.balance_snapshot import BalanceSnapshot
from src.persistence.balance_snapshot import write as write_balance


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "test-key")


@pytest.fixture
def _state_setup(tmp_path, monkeypatch):
    """state.config / state.analysis_store を最小構成で注入する。"""
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_provider = "yfinance"
    cfg.live_broker = None
    cfg.state_dir = tmp_path
    cfg.tradeable_instruments = []
    cfg.providers.twelvedata = None
    cfg.providers.mt5 = None
    cfg.providers.oanda = None
    cfg.price_monitor.interval_minutes = 5
    cfg.schedule.run_times = []

    # PositionManager は balance.json (balance_snapshot) を真実のソースとして
    # 読むため、テストで残高 ¥100,000 を期待する場合は事前に seed する。
    write_balance(
        tmp_path,
        BalanceSnapshot(
            balance=100_000.0,
            deposit=100_000.0,
            peak_balance=100_000.0,
            source="paper",
            fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ),
    )

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
    assert "mode" in data
    assert "live_broker" in data
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


def test_account_returns_internal_section(_state_setup):
    """/account は internal / mt5 / divergence の 3 セクションを返す (Task 10)。

    paper mode では mt5 / divergence は null。
    """
    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    # Top-level shape: exactly 3 keys
    assert set(data.keys()) == {"internal", "mt5", "divergence"}

    internal = data["internal"]
    # internal 必須フィールド
    for key in (
        "balance", "deposit", "peak_balance", "drawdown_pct",
        "pnl", "pnl_pct", "total_trades", "win_rate", "open_positions",
    ):
        assert key in internal, f"missing internal.{key}"
    # 初期状態: 残高 = 入金額、DD/PnL ともに 0、ポジションなし
    assert internal["balance"] == 100_000.0
    assert internal["deposit"] == 100_000.0
    assert internal["peak_balance"] == 100_000.0
    assert internal["drawdown_pct"] == 0.0
    assert internal["pnl"] == 0.0
    assert internal["pnl_pct"] == 0.0
    assert internal["open_positions"] == []

    # paper mode → mt5 / divergence は null
    assert data["mt5"] is None
    assert data["divergence"] is None


def test_account_returns_mt5_section_in_live_mode(_state_setup, monkeypatch):
    """live mode + bridge /account 成功時に mt5 と divergence が入る (Task 10)。"""
    _state_setup.mode = "live"
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="bridge-secret",
    )

    captured: dict = {}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "balance": 105_000.0,
                "equity": 104_500.0,
                "free_margin": 100_000.0,
                "margin": 4_500.0,
            }

    def _get(url, timeout=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return _R()

    monkeypatch.setattr("httpx.get", _get)

    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()

    # internal は常にあり
    assert data["internal"]["balance"] == 100_000.0

    # mt5 セクションが入る
    assert data["mt5"] is not None
    assert data["mt5"]["balance"] == 105_000.0
    assert data["mt5"]["equity"] == 104_500.0
    assert data["mt5"]["free_margin"] == 100_000.0
    assert data["mt5"]["margin"] == 4_500.0
    assert "fetched_at" in data["mt5"]

    # divergence セクションが入る (mt5 105k - internal 100k = +5k = +5%)
    assert data["divergence"] is not None
    assert data["divergence"]["balance_diff"] == 5_000.0
    assert data["divergence"]["balance_diff_pct"] == 5.0

    # X-Bridge-Api-Key ヘッダーが送信される
    assert captured["url"] == "http://x:8812/account"
    assert captured["headers"]["X-Bridge-Api-Key"] == "bridge-secret"


def test_account_mt5_null_when_bridge_unreachable(_state_setup, monkeypatch):
    """live mode で bridge 不通時は mt5 / divergence が null (Task 10)。"""
    _state_setup.mode = "live"
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    def _get(*a, **kw):
        import httpx as _httpx
        raise _httpx.ConnectError("down")

    monkeypatch.setattr("httpx.get", _get)

    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()

    # internal は常にあり (paper-default seed)
    assert data["internal"]["balance"] == 100_000.0
    # bridge 不通 → mt5 / divergence は null
    assert data["mt5"] is None
    assert data["divergence"] is None


def test_account_mt5_null_when_bridge_returns_invalid_json(_state_setup, monkeypatch):
    """live mode で bridge が壊れた JSON / 不完全レスポンスを返したら mt5 / divergence は null。"""
    _state_setup.mode = "live"
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("invalid json")

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _R())

    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mt5"] is None
    assert data["divergence"] is None


# ── /admin/halt /admin/resume プロキシ ──

def _client_post(path: str, **kw):
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", **kw.pop("headers", {})}
    return client.post(path, headers=headers, **kw)


def test_admin_halt_503_when_bridge_not_configured(_state_setup):
    """providers.mt5 = None で /admin/halt → 503。"""
    _state_setup.providers.mt5 = None
    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    assert resp.status_code == 503


def test_admin_halt_proxies_to_bridge(_state_setup, monkeypatch):
    """bridge へ POST /admin/halt がプロキシされ、レスポンスがそのまま返る。"""
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

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
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    def _post(*a, **kw):
        raise httpx.ConnectError("down")

    import httpx
    monkeypatch.setattr("httpx.post", _post)
    resp = _client_post("/admin/halt", json={"mode": "soft"})
    assert resp.status_code == 502


def test_admin_resume_proxies_to_bridge(_state_setup, monkeypatch):
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

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
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
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
