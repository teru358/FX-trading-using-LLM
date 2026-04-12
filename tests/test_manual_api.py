"""Manual REST API のテスト。"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_api_key():
    with patch.dict(os.environ, {"API_SECRET_KEY": "test-key"}):
        yield


@pytest.fixture
def client(tmp_path):
    from src.api._state import state
    from src.api.server import app
    from src.config.schema import AppConfig, TradingConfig
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager

    cfg = MagicMock(spec=AppConfig)
    cfg.trading = MagicMock(spec=TradingConfig)
    cfg.trading.trading_mode = "signal_only"
    cfg.trading.initial_balance = 500000.0
    cfg.manual_state_dir = tmp_path / "manual"

    manual_store = StateStore(tmp_path / "manual")
    manual_mgr = PositionManager(manual_store, 500000.0, context="ManualTest")

    state.config = cfg
    state.manual_position_mgr = manual_mgr

    from src.api.routes import manual
    if not any(getattr(r, 'path', '').startswith("/manual") for r in app.routes):
        app.include_router(manual.router)

    return TestClient(app)


HEADERS = {"X-API-Key": "test-key"}


def test_manual_list_empty(client):
    resp = client.get("/manual/list", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 500000.0
    assert data["open_positions"] == []


def test_manual_open_and_list(client):
    resp = client.post("/manual/open", headers=HEADERS, json={
        "pair": "USDJPY=X", "direction": "buy",
        "entry_price": 150.2, "position_size": 5000,
        "stop_loss": 149.8, "take_profit": 151.2,
    })
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]
    assert order_id

    resp = client.get("/manual/list", headers=HEADERS)
    positions = resp.json()["open_positions"]
    assert len(positions) == 1
    assert positions[0]["pair"] == "USDJPY=X"


def test_manual_close(client):
    resp = client.post("/manual/open", headers=HEADERS, json={
        "pair": "USDJPY=X", "direction": "buy",
        "entry_price": 150.0, "position_size": 5000,
        "stop_loss": 149.0, "take_profit": 152.0,
    })
    order_id = resp.json()["order_id"]

    resp = client.post(f"/manual/close/{order_id}", headers=HEADERS, json={
        "close_price": 151.0, "close_reason": "take_profit",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["realized_pnl"] > 0
    assert data["balance"] > 500000.0


def test_manual_balance(client):
    resp = client.post("/manual/balance", headers=HEADERS, json={"balance": 600000})
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 600000.0
    assert data["previous"] == 500000.0


def test_manual_close_not_found(client):
    resp = client.post("/manual/close/nonexistent", headers=HEADERS, json={
        "close_price": 151.0, "close_reason": "manual",
    })
    assert resp.status_code == 404
