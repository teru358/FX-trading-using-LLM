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

        def json(self):
            return {
                "ticket": 1, "symbol": "USDJPY", "side": "buy",
                "volume_lots": 0.01, "fill_price": 159.4,
                "sl": 158.8, "tp": 160.3, "time": "x",
                "dry_run": True, "magic": 0,
            }

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "post",
                        lambda url, json=None, timeout=None, headers=None:
                        captured.setdefault("headers", headers) or _Resp())
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
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812")
    pm = _make_pm()
    existing = Order.new("USDJPY=X", "buy", 159, 158, 160, 1000)
    pm.get_open_position.return_value = existing
    sig = _make_signal()
    assert adapter.execute_signal(sig, pm) is None


def test_empty_bridge_url_raises():
    with pytest.raises(ValueError, match="bridge_url is required"):
        Mt5BridgeBrokerAdapter(bridge_url="")
