from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server


class _FakeRuntime:
    def __init__(self, *, dry_run: bool) -> None:
        self.dry_run = dry_run


class _FakeClient:
    def __init__(self, *, connected: bool = True) -> None:
        self.is_connected = connected
        self.calls: list[tuple[int, float | None, float | None]] = []

    def modify_position_live(self, ticket: int, *, sl: float | None = None, tp: float | None = None) -> dict:
        self.calls.append((ticket, sl, tp))
        return {
            "ticket": ticket,
            "symbol": "USDJPY",
            "sl": sl,
            "tp": 161.0 if tp is None else tp,
            "retcode": 10009,
            "comment": "done",
            "dry_run": False,
        }

    def modify_position_dry_run(self, ticket: int, *, sl: float | None = None, tp: float | None = None) -> dict:
        self.calls.append((ticket, sl, tp))
        return {
            "ticket": ticket,
            "symbol": "DRYRUN",
            "sl": sl,
            "tp": tp,
            "retcode": None,
            "comment": "DRY_RUN: position modify not sent",
            "dry_run": True,
        }


def _patch_server(monkeypatch, *, client: _FakeClient, dry_run: bool) -> None:
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_runtime", _FakeRuntime(dry_run=dry_run))
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False))


def test_modify_position_accepts_sl_only_and_preserves_tp(monkeypatch):
    fake = _FakeClient()
    _patch_server(monkeypatch, client=fake, dry_run=False)

    req = server.ModifyPositionRequest(sl=159.5)
    resp = server.modify_position(123, req)

    assert resp.ticket == 123
    assert resp.symbol == "USDJPY"
    assert resp.sl == 159.5
    assert resp.tp == 161.0
    assert resp.retcode == 10009
    assert resp.comment == "done"
    assert resp.dry_run is False
    assert fake.calls == [(123, 159.5, None)]


def test_modify_position_rejects_empty_body(monkeypatch):
    _patch_server(monkeypatch, client=_FakeClient(), dry_run=False)

    req = server.ModifyPositionRequest()
    with pytest.raises(HTTPException) as exc:
        server.modify_position(123, req)

    assert exc.value.status_code == 400
    assert "sl or tp" in exc.value.detail


def test_modify_position_returns_503_when_mt5_disconnected(monkeypatch):
    _patch_server(monkeypatch, client=_FakeClient(connected=False), dry_run=False)

    req = server.ModifyPositionRequest(sl=159.5)
    with pytest.raises(HTTPException) as exc:
        server.modify_position(123, req)

    assert exc.value.status_code == 503
    assert exc.value.detail == "MT5 not connected"


def test_modify_position_dry_run_uses_dry_run_path(monkeypatch):
    fake = _FakeClient()
    _patch_server(monkeypatch, client=fake, dry_run=True)

    req = server.ModifyPositionRequest(sl=159.5)
    resp = server.modify_position(123, req)

    assert resp.dry_run is True
    assert resp.sl == 159.5
    assert fake.calls == [(123, 159.5, None)]


def test_modify_position_live_failure_returns_409(monkeypatch):
    class RejectingClient(_FakeClient):
        def modify_position_live(self, ticket: int, *, sl: float | None = None, tp: float | None = None) -> dict:
            raise RuntimeError("retcode=10016 Invalid stops")

    _patch_server(monkeypatch, client=RejectingClient(), dry_run=False)

    req = server.ModifyPositionRequest(sl=159.5)
    with pytest.raises(HTTPException) as exc:
        server.modify_position(123, req)

    assert exc.value.status_code == 409
    assert "Invalid stops" in exc.value.detail
