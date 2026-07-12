"""/status, /account の最小動作テスト (/health は /status へ統合済で削除)。

フル統合は手動で検証する。ここでは:
- /health は GET 404 (削除済の固定確認)
- /status がプロセス + halt + サブシステム健全性を一括で返す
- /account が残高・ポジション形式を返す (halt は含まない)
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


def test_health_endpoint_removed(_state_setup):
    """/health は /status に統合・削除済。GET は 404 を返す。"""
    resp = _client_get("/health")
    assert resp.status_code == 404


def test_status_returns_merged_payload(_state_setup):
    """/status は旧 /health フィールド + halt + サブシステム健全性をまとめて返す。"""
    resp = _client_get("/status")
    assert resp.status_code == 200
    data = resp.json()
    # プロセス基本情報 (旧 /health から統合)
    assert data["status"] == "ok"
    assert "mode" in data
    assert "live_broker" in data
    assert "started_at" in data
    assert "uptime_seconds" in data
    assert "now" in data
    assert "scheduler" in data
    # halt セクション (/account から移動)
    assert "halt" in data
    # サブシステム健全性
    assert "llm_circuit_breakers" in data
    assert "price_provider" in data
    assert "snapshots" in data
    assert "mt5_bridge" in data
    # MT5 bridge が未設定 → configured: False
    assert data["mt5_bridge"]["configured"] is False
    # /account 専用フィールドは含まない
    assert "balance" not in data
    assert "open_positions" not in data
    assert "internal" not in data


def test_status_returns_200_when_bridge_unreachable(_state_setup, monkeypatch):
    """bridge 不通でも /status は 200 を返し mt5_bridge.reachable=False で固定される。

    起動時の接続確認・use コマンドが /status を叩いても失敗しない設計を担保する。
    """
    _state_setup.mode = "live"
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    def _raise(*a, **kw):
        import httpx as _httpx
        raise _httpx.ConnectError("bridge down")

    monkeypatch.setattr("httpx.get", _raise)

    resp = _client_get("/status")
    assert resp.status_code == 200
    mt5b = resp.json()["mt5_bridge"]
    assert mt5b["configured"] is True
    assert mt5b["reachable"] is False
    assert "error" in mt5b


def test_account_returns_internal_section(_state_setup):
    """/account は internal / mt5 / divergence の 3 セクションを返す。

    paper mode では mt5 / divergence は null。halt は /status へ集約済。
    """
    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    # Top-level shape: exactly 3 keys (halt は /status へ移動)
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


def test_account_does_not_include_halt_section(_state_setup, tmp_path):
    """/account 応答に halt セクションは含まれない (/status へ集約済)。"""
    _state_setup.state_dir = tmp_path
    resp = _client_get("/account")
    assert resp.status_code == 200
    data = resp.json()
    assert "halt" not in data


def test_status_includes_halt_section_when_not_halted(_state_setup, tmp_path):
    """halt 状態でなくても halt セクションは常に dict として返る (null ではない)。"""
    _state_setup.state_dir = tmp_path  # halt.json 不在 → default 値

    resp = _client_get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "halt" in data
    halt = data["halt"]
    assert halt["soft_halted"] is False
    assert halt["auto_triggered"] is False
    assert halt["reason"] == ""
    assert halt["since"] is None
    assert halt["triggered_by"] == ""


def test_status_halt_section_when_auto_halted(_state_setup, tmp_path):
    """auto halt 中は halt セクションに soft_halted=true + 詳細が入る。"""
    from src.persistence import halt_state

    _state_setup.state_dir = tmp_path
    halt_state.trigger_auto(tmp_path, "balance divergence >5% for 30min", "balance_diff")

    resp = _client_get("/status")
    assert resp.status_code == 200
    halt = resp.json()["halt"]
    assert halt["soft_halted"] is True
    assert halt["auto_triggered"] is True
    assert halt["triggered_by"] == "balance_diff"
    assert halt["reason"] == "balance divergence >5% for 30min"
    assert halt["since"]


def test_status_halt_derived_fields_when_halted(_state_setup, tmp_path):
    """halt 中に blocks_new_orders=True、allows_position_management=True が導出される。"""
    from src.persistence import halt_state

    _state_setup.state_dir = tmp_path
    halt_state.trigger_manual(tmp_path, reason="test")

    resp = _client_get("/status")
    assert resp.status_code == 200
    halt = resp.json()["halt"]
    assert halt["soft_halted"] is True
    assert halt["blocks_new_orders"] is True
    assert halt["allows_position_management"] is True


def test_status_halt_derived_fields_when_not_halted(_state_setup, tmp_path):
    """halt 解除中も blocks_new_orders=False で形を一定に保つ。"""
    _state_setup.state_dir = tmp_path  # halt.json 不在

    resp = _client_get("/status")
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


# ── /tech エンドポイント (Task 3.3: latest_collect / latest_ok 分離) ──


def test_tech_endpoint_returns_latest_collect_and_latest_ok(
    _state_setup, tmp_path,
):
    """/tech は latest_collect (全 status) と latest_ok (ok のみ) を返す。"""
    from datetime import timedelta
    from types import SimpleNamespace
    from src.data.analysis_store import AnalysisStore
    from src.analysis.technical_snapshot_data import TechnicalSnapshotData
    from src.utils.clock import db_now

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store

    inst = SimpleNamespace(
        symbol="USDJPY=X", display_name="USD/JPY", mode="trade",
    )
    state.config.tradeable_instruments = [inst]
    state.config.watch_only_instruments = []

    # 古い ok + 直近 sentinel を投入
    store.add_snapshot(TechnicalSnapshotData(
        pair="USDJPY=X", direction_bias="long", bias_score=0.2, confidence=0.6,
        mtf_alignment=0.8, tf_scores={"long": {"score": 0.2, "direction": "long"}},
        components={"sma": 0.1}, patterns=["engulfing_bullish"],
        analyzed_at=db_now() - timedelta(hours=4),
    ))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="latest bar 7h ago")

    resp = _client_get("/tech")
    assert resp.status_code == 200
    body = resp.json()
    assert "snapshots" in body
    snaps = body["snapshots"]
    assert len(snaps) == 1
    s = snaps[0]
    assert s["symbol"] == "USDJPY=X"
    assert s["latest_collect"] is not None
    assert s["latest_collect"]["collect_status"] == "stale_price"
    assert s["latest_collect"]["reason"] == "latest bar 7h ago"
    assert s["latest_ok"] is not None
    assert s["latest_ok"]["direction_bias"] == "long"
    assert s["latest_ok"]["bias_score"] == 0.2
    assert s["latest_ok"]["confidence"] == 0.6
    assert s["latest_ok"]["mtf_alignment"] == 0.8
    assert s["latest_ok"]["patterns"] == ["engulfing_bullish"]


def test_tech_endpoint_returns_null_when_no_data(
    _state_setup, tmp_path,
):
    """データなし → latest_collect / latest_ok とも null。"""
    from types import SimpleNamespace
    from src.data.analysis_store import AnalysisStore

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store

    inst = SimpleNamespace(
        symbol="GBPUSD=X", display_name="GBP/USD", mode="trade",
    )
    state.config.tradeable_instruments = [inst]
    state.config.watch_only_instruments = []

    resp = _client_get("/tech")
    assert resp.status_code == 200
    body = resp.json()
    s = body["snapshots"][0]
    assert s["latest_collect"] is None
    assert s["latest_ok"] is None


def test_tech_endpoint_survives_corrupt_patterns_json(
    _state_setup, tmp_path,
):
    """破損した patterns_json でも /tech は 500 でなく 200 + patterns=[] を返す。"""
    from datetime import timedelta
    from types import SimpleNamespace

    from sqlalchemy import text

    from src.data.analysis_store import AnalysisStore
    from src.analysis.technical_snapshot_data import TechnicalSnapshotData
    from src.utils.clock import db_now

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store

    inst = SimpleNamespace(
        symbol="USDJPY=X", display_name="USD/JPY", mode="trade",
    )
    state.config.tradeable_instruments = [inst]
    state.config.watch_only_instruments = []

    store.add_snapshot(TechnicalSnapshotData(
        pair="USDJPY=X", direction_bias="long", bias_score=0.2, confidence=0.6,
        mtf_alignment=0.8, tf_scores={"long": {"score": 0.2, "direction": "long"}},
        components={"sma": 0.1}, patterns=["engulfing_bullish"],
        analyzed_at=db_now() - timedelta(hours=4),
    ))
    # 破損 JSON を直接注入 (手動デバッグ / 部分書き込みを再現)
    with store._engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE technical_snapshots SET patterns_json = :bad "
                "WHERE symbol = :sym"
            ),
            {"bad": "{not valid", "sym": "USDJPY=X"},
        )
        conn.commit()

    resp = _client_get("/tech")
    assert resp.status_code == 200
    body = resp.json()
    s = body["snapshots"][0]
    assert s["latest_ok"] is not None
    assert s["latest_ok"]["patterns"] == []


def test_tech_endpoint_valid_patterns_json_still_decodes(
    _state_setup, tmp_path,
):
    """正常な patterns_json は従来どおり list へデコードされる (回帰防止)。"""
    from datetime import timedelta
    from types import SimpleNamespace

    from src.data.analysis_store import AnalysisStore
    from src.analysis.technical_snapshot_data import TechnicalSnapshotData
    from src.utils.clock import db_now

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store

    inst = SimpleNamespace(
        symbol="EURUSD=X", display_name="EUR/USD", mode="trade",
    )
    state.config.tradeable_instruments = [inst]
    state.config.watch_only_instruments = []

    store.add_snapshot(TechnicalSnapshotData(
        pair="EURUSD=X", direction_bias="short", bias_score=-0.3, confidence=0.5,
        mtf_alignment=0.7, tf_scores={"short": {"score": -0.3, "direction": "short"}},
        components={"sma": -0.1}, patterns=["doji", "hammer"],
        analyzed_at=db_now() - timedelta(hours=2),
    ))

    resp = _client_get("/tech")
    assert resp.status_code == 200
    body = resp.json()
    s = body["snapshots"][0]
    assert s["latest_ok"]["patterns"] == ["doji", "hammer"]


def test_status_includes_reconciliation_skipped_on_success(_state_setup, tmp_path, monkeypatch):
    """bridge 応答成功時の /status に reconciliation_skipped_consecutive が含まれる。"""
    from src.persistence import reconciliation_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    # 永続カウンタに値を仕込む
    reconciliation_state.increment_skip(tmp_path)
    reconciliation_state.increment_skip(tmp_path)
    reconciliation_state.increment_skip(tmp_path)

    class _OK:
        is_success = True
        def json(self): return {"mt5_connected": True}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _OK())

    resp = _client_get("/status")
    assert resp.status_code == 200
    mt5b = resp.json()["mt5_bridge"]
    assert mt5b["reconciliation_skipped_consecutive"] == 3


def test_status_includes_reconciliation_skipped_on_bridge_failure(_state_setup, tmp_path, monkeypatch):
    """bridge 不通時の /status 早期 return にも reconciliation_skipped_consecutive が含まれる。

    bridge 不通こそが診断したい状況なので、失敗 return でも必ず見える必要がある。
    """
    from src.persistence import reconciliation_state

    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    # 永続カウンタに値を仕込む
    for _ in range(7):
        reconciliation_state.increment_skip(tmp_path)

    def _raise(*a, **kw):
        import httpx as _httpx
        raise _httpx.ConnectError("bridge down")

    monkeypatch.setattr("httpx.get", _raise)

    resp = _client_get("/status")
    assert resp.status_code == 200
    mt5b = resp.json()["mt5_bridge"]
    # 早期 return path
    assert mt5b["configured"] is True
    assert mt5b["reachable"] is False
    assert "error" in mt5b
    # 不通時にも skip 連続回数が見えること (本タスクの本丸)
    assert mt5b["reconciliation_skipped_consecutive"] == 7


def test_status_reconciliation_field_zero_when_counter_unused(_state_setup, tmp_path, monkeypatch):
    """reconciliation.json 不在時は 0 を返す (null ではない)。"""
    _state_setup.mode = "live"
    _state_setup.state_dir = tmp_path
    _state_setup.providers.mt5 = MagicMock(
        bridge_url="http://x:8812", api_key="",
    )

    class _OK:
        is_success = True
        def json(self): return {"mt5_connected": True}

    monkeypatch.setattr("httpx.get", lambda *a, **kw: _OK())

    resp = _client_get("/status")
    assert resp.status_code == 200
    mt5b = resp.json()["mt5_bridge"]
    assert mt5b["reconciliation_skipped_consecutive"] == 0
