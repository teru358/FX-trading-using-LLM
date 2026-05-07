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
    # Top-level shape: exactly 4 keys
    assert set(data.keys()) == {"internal", "mt5", "divergence", "halt"}

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
    assert "snapshot_fetched_at" in data["mt5"]

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


def test_account_includes_halt_section_when_not_halted(_state_setup, tmp_path):
    """halt 状態でなくても halt セクションは常に dict として返る (null ではない)。"""
    _state_setup.state_dir = tmp_path  # halt.json 不在 → default 値

    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    assert "halt" in data
    halt = data["halt"]
    assert halt["soft_halted"] is False
    assert halt["auto_triggered"] is False
    assert halt["reason"] == ""
    assert halt["since"] is None
    assert halt["triggered_by"] == ""


def test_account_halt_section_when_auto_halted(_state_setup, tmp_path):
    """auto halt 中は halt セクションに soft_halted=true + 詳細が入る。"""
    from src.persistence import halt_state

    _state_setup.state_dir = tmp_path
    halt_state.trigger_auto(tmp_path, "5 consecutive /health failures", "heartbeat")

    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    halt = data["halt"]
    assert halt["soft_halted"] is True
    assert halt["auto_triggered"] is True
    assert halt["triggered_by"] == "heartbeat"
    assert halt["reason"] == "5 consecutive /health failures"
    assert halt["since"]


def test_account_halt_section_has_derived_fields_when_halted(_state_setup, tmp_path):
    """halt 中に blocks_new_orders=True、allows_position_management=True が導出される。"""
    from src.persistence import halt_state

    _state_setup.state_dir = tmp_path
    halt_state.trigger_manual(tmp_path, reason="test")

    resp = _client_get("/account")
    assert resp.status_code == 200
    halt = resp.json()["halt"]
    assert halt["soft_halted"] is True
    assert halt["blocks_new_orders"] is True
    assert halt["allows_position_management"] is True


def test_account_halt_section_derived_fields_when_not_halted(_state_setup, tmp_path):
    """halt 解除中も blocks_new_orders=False で形を一定に保つ。"""
    _state_setup.state_dir = tmp_path  # halt.json 不在

    resp = _client_get("/account")
    halt = resp.json()["halt"]
    assert halt["soft_halted"] is False
    assert halt["blocks_new_orders"] is False
    assert halt["allows_position_management"] is True


# ── /admin/halt /admin/resume プロキシ ──

def _client_post(path: str, **kw):
    client = TestClient(app)
    headers = {"X-API-Key": "test-key", **kw.pop("headers", {})}
    return client.post(path, headers=headers, **kw)


def test_admin_halt_503_when_bridge_not_configured(_state_setup):
    """providers.mt5 = None で /admin/halt → 503。"""
    _state_setup.mode = "live"
    _state_setup.providers.mt5 = None
    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    assert resp.status_code == 503


def test_admin_halt_proxies_to_bridge(_state_setup, monkeypatch):
    """bridge へ POST /admin/halt がプロキシされ、レスポンスがそのまま返る。"""
    _state_setup.mode = "live"
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
    assert body["bridge"]["soft_halted"] is True   # mode=soft now wraps in {finance, bridge}
    assert body["finance"]["soft_halted"] is True   # finance state also set
    assert captured["url"] == "http://x:8812/admin/halt"
    assert captured["json"]["mode"] == "soft"
    assert captured["json"]["reason"] == "test"


def test_admin_resume_proxies_to_bridge(_state_setup, monkeypatch, tmp_path):
    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    class _ResumeOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"dry_run": False, "soft_halted": False, "is_hard_halted": False}

    class _StatusOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"soft_halted": False, "mt5_connected": True,
                    "accepts_new_orders": True, "is_hard_halted": False,
                    "dry_run": False}

    def _fake_get(url, **kw):
        if url.endswith("/admin/status"):
            return _StatusOK()
        return _HealthOK()
    monkeypatch.setattr("httpx.get", _fake_get)
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _ResumeOK())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bridge_status"]["accepts_new_orders"] is True
    assert body["finance"]["soft_halted"] is False


def test_admin_resume_403_when_hard_halted(_state_setup, monkeypatch, tmp_path):
    """bridge が 403 を返したら finance も 403 を返す (hard halt 中の resume 拒否)。"""
    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    import httpx

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    class _Req:
        url = "http://x:8812/admin/resume"

    class _R:
        status_code = 403
        text = "hard halt cannot be resumed remotely"
        def raise_for_status(self):
            raise httpx.HTTPStatusError("403", request=_Req(), response=self)
        def json(self):
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _HealthOK())
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R())
    resp = _client_post("/admin/resume")
    assert resp.status_code == 403


def test_admin_halt_rejected_in_paper_mode(_state_setup):
    """paper モードでは /admin/halt は 400 を返す (bridge が無いため)。"""
    # _state_setup fixture のデフォルトは mode="paper"
    assert _state_setup.mode == "paper"
    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    assert resp.status_code == 400
    assert "paper" in resp.json()["detail"].lower()


def test_admin_resume_rejected_in_paper_mode(_state_setup):
    """paper モードでは /admin/resume は 400 を返す (bridge が無いため)。"""
    assert _state_setup.mode == "paper"
    resp = _client_post("/admin/resume")
    assert resp.status_code == 400
    assert "paper" in resp.json()["detail"].lower()


def test_admin_halt_updates_finance_state(_state_setup, monkeypatch, tmp_path):
    """POST /admin/halt mode=soft で finance halt.json が更新される。"""
    from src.persistence import halt_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    class _R:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"soft_halted": True}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R())

    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "manual test"})
    assert resp.status_code == 200

    # finance halt.json も更新されている
    state = halt_state.read(tmp_path)
    assert state.soft_halted is True
    assert state.auto_triggered is False
    assert state.reason == "manual test"
    assert state.triggered_by == "manual"

    # レスポンスに finance / bridge 両セクションが含まれる
    body = resp.json()
    assert "finance" in body
    assert "bridge" in body
    assert body["finance"]["soft_halted"] is True


def test_admin_halt_updates_finance_state_even_when_bridge_post_fails(
    _state_setup, monkeypatch, tmp_path
):
    """bridge POST が失敗しても finance state は確定し、502 ではなく 200 を返す。"""
    from src.persistence import halt_state
    import httpx as _httpx

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **kw: (_ for _ in ()).throw(_httpx.ConnectError("down")),
    )

    resp = _client_post("/admin/halt", json={"mode": "soft", "reason": "test"})
    # finance state は更新済 → 200 を返す (bridge エラーはレスポンスに含める)
    assert resp.status_code == 200
    state = halt_state.read(tmp_path)
    assert state.soft_halted is True
    body = resp.json()
    assert body["finance"]["soft_halted"] is True
    assert "error" in body["bridge"]


def test_admin_halt_hard_remains_proxy_only(_state_setup, monkeypatch, tmp_path):
    """mode=hard は本仕様 out-of-scope。finance state を更新しない。"""
    from src.persistence import halt_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    class _R:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"hard_halted": True}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R())

    resp = _client_post("/admin/halt", json={"mode": "hard", "reason": "emergency"})
    assert resp.status_code == 200

    # mode=hard では finance state は更新されない
    state = halt_state.read(tmp_path)
    assert state.soft_halted is False


def test_admin_halt_hard_fallbacks_to_finance_soft_halt_on_bridge_failure(
    _state_setup, monkeypatch, tmp_path,
):
    """mode=hard で bridge POST が失敗したら finance soft halt を立てて 502。"""
    from src.persistence import halt_state
    import httpx as _httpx

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **kw: (_ for _ in ()).throw(_httpx.ConnectError("bridge down")),
    )

    resp = _client_post("/admin/halt", json={"mode": "hard", "reason": "manual emergency"})
    assert resp.status_code == 502

    # finance soft halt が立つ
    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "manual"  # trigger_manual を使う
    assert "manual hard halt delivery failed" in s.reason


def test_admin_halt_hard_fallbacks_to_finance_soft_halt_on_5xx(
    _state_setup, monkeypatch, tmp_path,
):
    """mode=hard で bridge が 5xx を返したら finance soft halt を立てて元の status code で返す。"""
    from src.persistence import halt_state
    import httpx as _httpx

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    class _Req:
        url = "http://x:8812/admin/halt"

    class _R500:
        status_code = 500
        text = "internal error"
        def raise_for_status(self):
            raise _httpx.HTTPStatusError("500", request=_Req(), response=self)
        def json(self):
            return {}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _R500())

    resp = _client_post("/admin/halt", json={"mode": "hard", "reason": "test"})
    # bridge が返したステータスコードで応答
    assert resp.status_code == 500

    # finance soft halt は立っている (fail-safe)
    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "manual"
    assert "manual hard halt delivery failed" in s.reason
    assert "500" in s.reason

    # レスポンスに finance state + bridge 情報が含まれる
    body = resp.json()
    detail = body["detail"]
    assert detail["finance"]["soft_halted"] is True
    assert detail["bridge_status"] == 500
    assert detail["bridge_body"] == "internal error"


def test_admin_resume_rejects_when_bridge_health_unreachable(
    _state_setup, monkeypatch, tmp_path
):
    """bridge /health が不通の場合、resume は 503 で拒否し halt は維持される。"""
    from src.persistence import halt_state
    import httpx as _httpx

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")

    # 事前に halt 状態にしておく
    halt_state.trigger_manual(tmp_path, "preexisting")

    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **kw: (_ for _ in ()).throw(_httpx.ConnectError("down")),
    )

    resp = _client_post("/admin/resume")
    assert resp.status_code == 503
    assert "bridge" in resp.json()["detail"].lower()

    # halt は維持されている
    state = halt_state.read(tmp_path)
    assert state.soft_halted is True


def test_admin_resume_clears_finance_state_when_bridge_health_ok(
    _state_setup, monkeypatch, tmp_path
):
    """bridge /health OK + resume POST OK + status ready → finance halt.json をクリア。"""
    from src.persistence import halt_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    halt_state.trigger_auto(tmp_path, "5 failures", "heartbeat")

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    class _ResumeOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"soft_halted": False, "accepts_new_orders": True}

    class _StatusOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"soft_halted": False, "mt5_connected": True,
                    "accepts_new_orders": True}

    def _fake_get(url, **kw):
        if url.endswith("/admin/status"):
            return _StatusOK()
        return _HealthOK()
    monkeypatch.setattr("httpx.get", _fake_get)
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _ResumeOK())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 200

    state = halt_state.read(tmp_path)
    assert state.soft_halted is False

    body = resp.json()
    assert body["finance"]["soft_halted"] is False
    assert body["bridge"]["soft_halted"] is False
    assert body["bridge_status"]["accepts_new_orders"] is True


def test_admin_resume_503_when_resume_post_fails(_state_setup, monkeypatch, tmp_path):
    """bridge /health OK だが /admin/resume POST が失敗 → 503 で拒否、halt 維持。"""
    from src.persistence import halt_state
    import httpx as _httpx

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    halt_state.trigger_manual(tmp_path, "pre")

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _HealthOK())
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **kw: (_ for _ in ()).throw(_httpx.ConnectError("post down")),
    )

    resp = _client_post("/admin/resume")
    assert resp.status_code == 503
    # halt 維持
    assert halt_state.read(tmp_path).soft_halted is True


def test_admin_resume_503_when_health_ok_but_mt5_not_connected(
    _state_setup, monkeypatch, tmp_path,
):
    """bridge /health 200 だが mt5_connected=false → 503, halt 維持。"""
    from src.persistence import halt_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    halt_state.trigger_manual(tmp_path, "pre")

    class _HealthMt5Down:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": False}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _HealthMt5Down())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 503
    assert "MT5 is not connected" in resp.json()["detail"]
    assert halt_state.read(tmp_path).soft_halted is True


def test_admin_resume_503_when_status_not_ready_in_live(
    _state_setup, monkeypatch, tmp_path,
):
    """live モード時、/admin/status.accepts_new_orders=false → 503, halt 維持。"""
    from src.persistence import halt_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    halt_state.trigger_manual(tmp_path, "pre")

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    class _ResumeOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"resumed": True}

    class _StatusNotReady:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"soft_halted": False, "mt5_connected": True,
                    "accepts_new_orders": False}  # ← live で reject される

    def _fake_get(url, **kw):
        if url.endswith("/admin/status"):
            return _StatusNotReady()
        return _HealthOK()
    monkeypatch.setattr("httpx.get", _fake_get)
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _ResumeOK())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 503
    # halt 維持
    assert halt_state.read(tmp_path).soft_halted is True


def test_admin_resume_succeeds_in_live_test_when_dry_run_but_mt5_connected(
    _state_setup, monkeypatch, tmp_path,
):
    """live_test モード時、accepts_new_orders=false (DRY_RUN) でも soft_halted=false + mt5_connected=true なら成功。"""
    from src.persistence import halt_state

    _state_setup.mode = "live_test"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(bridge_url="http://x:8812", api_key="")
    halt_state.trigger_manual(tmp_path, "pre")

    class _HealthOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"mt5_connected": True}

    class _ResumeOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"resumed": True}

    class _StatusDryRun:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"soft_halted": False, "mt5_connected": True,
                    "accepts_new_orders": False, "dry_run": True}  # live_test では OK

    def _fake_get(url, **kw):
        if url.endswith("/admin/status"):
            return _StatusDryRun()
        return _HealthOK()
    monkeypatch.setattr("httpx.get", _fake_get)
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _ResumeOK())

    resp = _client_post("/admin/resume")
    assert resp.status_code == 200
    assert halt_state.read(tmp_path).soft_halted is False
