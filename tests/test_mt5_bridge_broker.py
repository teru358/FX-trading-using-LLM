"""Mt5BridgeBrokerAdapter のユニットテスト。

httpx をモックし、シグナル種別 (hold / buy)、bridge 障害、既存ポジション
ありの各分岐で正しく動作することを検証する。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import httpx
import pytest

from src.signals.signal_combiner import NewsSentiment, PriceAnalysis, TradeSignal
from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
from src.trading.position_manager import Order, PositionManager


def _make_signal(action: str = "buy", pair: str = "USDJPY=X") -> TradeSignal:
    return TradeSignal(
        pair=pair, action=action, predicted_direction="bullish",
        combined_score=0.7, confidence=0.8,
        entry_price=159.30, stop_loss=158.80, take_profit=160.30,
        position_size=1000.0, signal_reason="test", detail_reason="test",
        news=MagicMock(spec=NewsSentiment), price=MagicMock(spec=PriceAnalysis),
        generated_at=datetime.now(),
    )


def _make_pm(open_positions=None, closed_trades=None,
             initial_balance: float = 100_000.0) -> MagicMock:
    pm = MagicMock(spec=PositionManager)
    pm.get_open_position.return_value = None
    pm.get_open_positions_by_pair.return_value = []
    account = MagicMock()
    account.open_positions = open_positions or []
    account.closed_trades = closed_trades or []
    account.initial_balance = initial_balance
    pm.get_account_state.return_value = account
    return pm


def test_execute_signal_hold_returns_none():
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812")
    sig = _make_signal(action="hold")
    pm = _make_pm()
    assert adapter.execute_signal(sig, pm) is None
    pm.open_position.assert_not_called()


def test_execute_signal_posts_order_and_records(monkeypatch):
    captured: dict = {}

    class _Resp:
        is_success = True
        status_code = 200

        def json(self):
            return {
                "ticket": 1234567890,
                "symbol": "USDJPY", "side": "buy", "volume_lots": 0.01,
                "fill_price": 159.469, "sl": 158.80, "tp": 160.30,
                "time": "2026-04-26T...", "dry_run": True, "magic": 12345,
            }

        def raise_for_status(self):
            pass

    def _post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)

    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://example:8812",
        lot_size_units=100_000, magic_number=12345,
    )
    sig = _make_signal(action="buy")
    order = adapter.execute_signal(sig, pm)

    assert order is not None
    assert order.order_id.startswith("mt5:")
    assert order.pair == "USDJPY=X"             # 内部正規形が保たれる
    assert order.direction == "buy"
    assert order.entry_price == pytest.approx(159.469)   # bridge fill_price
    assert captured["url"] == "http://example:8812/order"
    assert captured["json"]["symbol"] == "USDJPY"           # 送信時は MT5 形式
    assert captured["json"]["volume_lots"] == pytest.approx(0.01)
    assert captured["json"]["magic"] == 12345
    pm.open_position.assert_called_once()


def test_execute_signal_with_api_key_sends_header(monkeypatch):
    captured: dict = {}

    class _Resp:
        is_success = True
        status_code = 200

        def json(self):
            return {
                "ticket": 1, "symbol": "USDJPY", "side": "buy",
                "volume_lots": 0.01, "fill_price": 159.4,
                "sl": 158.8, "tp": 160.3, "time": "x",
                "dry_run": True, "magic": 0,
            }

        def raise_for_status(self):
            pass

    def _post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://example:8812", api_key="secret123",
    )
    adapter.execute_signal(_make_signal(action="buy"), pm)
    assert captured["headers"]["X-Bridge-Api-Key"] == "secret123"


def test_execute_signal_handles_bridge_error_gracefully(monkeypatch, caplog):
    def _post(*a, **kw):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr(httpx, "post", _post)

    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812")
    sig = _make_signal(action="buy")
    with caplog.at_level("ERROR"):
        order = adapter.execute_signal(sig, pm)
    assert order is None
    assert any("bridge" in r.message.lower() for r in caplog.records)
    pm.open_position.assert_not_called()


def test_existing_position_returns_none():
    """scale-in disabled (default) で既存ポジがある場合はスキップする。"""
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812")
    pm = _make_pm()
    existing = Order.new("USDJPY=X", "buy", 159, 158, 160, 1000)
    pm.get_open_positions_by_pair.return_value = [existing]
    sig = _make_signal()
    assert adapter.execute_signal(sig, pm) is None


# ── Phase 3b: HTTP status routing ──

def test_execute_signal_handles_422_margin_insufficient(monkeypatch, caplog):
    """422: 証拠金不足 → 警告ログ、order=None。"""
    class _Resp:
        is_success = False
        status_code = 422
        text = '{"detail":"insufficient margin"}'
        def json(self):
            return {"detail": "insufficient margin: required=6695 free=5200"}

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812")
    with caplog.at_level("WARNING"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)

    assert order is None
    assert any("margin" in r.message.lower() for r in caplog.records)


def test_execute_signal_handles_423_soft_halt(monkeypatch, caplog):
    """423: soft halted → info ログ、order=None。"""
    class _Resp:
        is_success = False
        status_code = 423
        text = '{"detail":"soft halted"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812")
    with caplog.at_level("INFO"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)

    assert order is None
    assert any("halted" in r.message.lower() for r in caplog.records)


def test_execute_signal_handles_409_order_rejected(monkeypatch, caplog):
    """409: bridge が retcode で reject した時の挙動。"""
    class _Resp:
        is_success = False
        status_code = 409
        text = "order rejected: retcode=10006"

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812")
    with caplog.at_level("WARNING"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)
    assert order is None
    assert any("reject" in r.message.lower() for r in caplog.records)


def test_execute_signal_partial_fill_modifies_position_size(monkeypatch):
    """部分約定: actual lots に応じて Order.position_size を修正。"""
    class _Resp:
        is_success = True
        status_code = 200
        def json(self):
            return {
                "ticket": 1234567890,
                "symbol": "USDJPY", "side": "buy",
                "volume_lots": 0.0075,    # 要求 0.01 → 約定 0.0075 (75%)
                "fill_price": 159.469,
                "sl": 158.80, "tp": 160.30,
                "time": "x", "dry_run": False, "magic": 12345,
            }
        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000,
    )
    sig = _make_signal(action="buy")    # signal.position_size = 1000.0

    order = adapter.execute_signal(sig, pm)

    assert order is not None
    # 1000 * (0.0075 / 0.01) = 750
    assert order.position_size == pytest.approx(750.0, rel=0.01)


def test_empty_bridge_url_raises():
    with pytest.raises(ValueError, match="bridge_url is required"):
        Mt5BridgeBrokerAdapter(bridge_url="")
