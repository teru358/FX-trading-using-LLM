"""Mt5BridgeBrokerAdapter の reconciliation + safe_close ユニットテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
from src.trading.position_manager import Order


def _mt5_order(ticket: int, pair: str = "USDJPY=X",
               position_size: float = 1000.0) -> Order:
    o = Order.new(pair, "buy", 159.0, 158.0, 160.0, position_size)
    o.order_id = f"mt5:{ticket}"
    return o


def _mock_resp(positions_payload):
    class _Resp:
        is_success = True
        def json(self):
            return positions_payload
        def raise_for_status(self):
            pass
    return _Resp()


# ── 完全 close ──

def test_reconcile_full_close_when_missing_from_mt5(monkeypatch, tmp_path):
    """完全 close: 内部 open かつ MT5 不在 → 内部 close。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    open_pos = [_mt5_order(111), _mt5_order(222, pair="EURUSD=X")]
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 222, "symbol": "EURUSD", "volume": 0.01, "magic": 12345}]
    ))
    pm = MagicMock()
    closed_order = _mt5_order(111)
    pm.close_position.return_value = closed_order
    result = adapter.check_and_close_positions(
        open_pos, {"USDJPY=X": 158.0, "EURUSD=X": 1.07}, pm,
    )
    assert len(result) == 1
    pm.close_position.assert_called_once_with("mt5:111", 158.0, "server_sl_tp")


def test_reconcile_skips_paper_orders(monkeypatch, tmp_path):
    """paper orders (mt5: prefix なし) は MT5 ポジ無くても close されない。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    paper_order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 160.0, 1000)
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp([]))
    pm = MagicMock()
    result = adapter.check_and_close_positions([paper_order], {"USDJPY=X": 158.0}, pm)
    assert result == []
    pm.close_position.assert_not_called()


def test_reconcile_bridge_unreachable_returns_empty(monkeypatch, caplog, tmp_path):
    """bridge 不通: noop, 空リスト返却 + warning。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    pm = MagicMock()
    with caplog.at_level("WARNING"):
        result = adapter.check_and_close_positions([_mt5_order(111)], {}, pm)
    assert result == []
    pm.close_position.assert_not_called()
    assert any("unreachable" in r.message.lower() for r in caplog.records)


# ── 部分 close ──

def test_reconcile_partial_close_within_normal_range(monkeypatch, tmp_path):
    """部分 close 1-30% (正常範囲): adjust_position_size 呼出。

    25% 削減を例にとる (0.01 → 0.0075 lot)。30% は HIGH threshold で hard halt 側
    なので、normal range の検証には strictly < 30% の値を使う。
    """
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    open_pos = [_mt5_order(111, position_size=1000.0)]    # 0.01 lot
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 111, "symbol": "USDJPY", "volume": 0.0075, "magic": 12345}]
    ))
    pm = MagicMock()
    adapter.check_and_close_positions(open_pos, {}, pm)
    pm.adjust_position_size.assert_called_once()
    args = pm.adjust_position_size.call_args
    assert args[0][0] == "mt5:111"
    assert args[0][1] == pytest.approx(750.0, rel=0.01)


def test_reconcile_partial_close_below_threshold_ignored(monkeypatch, tmp_path):
    """端数 < 1%: 無視。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    open_pos = [_mt5_order(111, position_size=1000.0)]
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 111, "symbol": "USDJPY", "volume": 0.00995, "magic": 12345}]
    ))
    pm = MagicMock()
    adapter.check_and_close_positions(open_pos, {}, pm)
    pm.adjust_position_size.assert_not_called()
    pm.close_position.assert_not_called()


def test_reconcile_volume_mismatch_above_threshold_triggers_halt(monkeypatch, tmp_path):
    """volume 差 ≥ 30%: hard halt。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    open_pos = [_mt5_order(111, position_size=1000.0)]
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 111, "symbol": "USDJPY", "volume": 0.005, "magic": 12345}]
    ))
    halt_called: list[str] = []
    monkeypatch.setattr(adapter, "_trigger_hard_halt", lambda r: halt_called.append(r))
    pm = MagicMock()
    adapter.check_and_close_positions(open_pos, {}, pm)
    assert len(halt_called) == 1
    assert "mismatch" in halt_called[0].lower()
    pm.adjust_position_size.assert_not_called()


def test_reconcile_orphan_bot_magic_triggers_halt(monkeypatch, tmp_path):
    """MT5 にあるが内部に無い (bot magic): hard halt。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 999, "symbol": "USDJPY", "volume": 0.01, "magic": 12345}]
    ))
    halt_called: list[str] = []
    monkeypatch.setattr(adapter, "_trigger_hard_halt", lambda r: halt_called.append(r))
    pm = MagicMock()
    adapter.check_and_close_positions([], {}, pm)
    assert len(halt_called) == 1
    assert "orphan" in halt_called[0].lower()


def test_reconcile_external_magic_ignored_and_cached(monkeypatch, tmp_path):
    """他 magic ポジ: 干渉せず、表示用キャッシュに保持。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 5555, "symbol": "USDJPY", "volume": 0.05, "magic": 99999}]
    ))
    halt_called: list[str] = []
    monkeypatch.setattr(adapter, "_trigger_hard_halt", lambda r: halt_called.append(r))
    pm = MagicMock()
    result = adapter.check_and_close_positions([], {}, pm)
    assert result == []
    assert halt_called == []    # 干渉なし
    assert len(adapter._cached_external_positions) == 1


# ── safe_close ──

def test_safe_close_succeeds_first_attempt(monkeypatch, tmp_path):
    """safe_close: 1 回目で確認できれば成功。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _mock_resp({}))
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp([]))    # 既に消えている
    monkeypatch.setattr(
        "src.trading.mt5_bridge_broker._time.sleep", lambda _: None,
    )
    assert adapter.safe_close(111) is True


def test_safe_close_triggers_halt_after_3_retries(monkeypatch, tmp_path):
    """safe_close: 3 回失敗で hard halt。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _mock_resp({}))
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _mock_resp(
        [{"ticket": 111, "symbol": "USDJPY", "volume": 0.01, "magic": 12345}]
    ))
    monkeypatch.setattr(
        "src.trading.mt5_bridge_broker._time.sleep", lambda _: None,
    )
    halt_called: list[str] = []
    monkeypatch.setattr(adapter, "_trigger_hard_halt", lambda r: halt_called.append(r))
    result = adapter.safe_close(111, max_retries=3)
    assert result is False
    assert len(halt_called) == 1
    assert "still open" in halt_called[0].lower()


# ── 能動 close (BrokerAdapter.close_position 経由) ──

def test_active_close_mt5_ticket_calls_safe_close(monkeypatch, tmp_path):
    """mt5: prefix の order_id は safe_close 経由で MT5 へ close 指令を送る。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    safe_close_called: list[int] = []
    monkeypatch.setattr(
        adapter, "safe_close",
        lambda ticket, max_retries=3: safe_close_called.append(ticket) or True,
    )
    pm = MagicMock()
    closed_order = _mt5_order(111)
    pm.close_position.return_value = closed_order

    result = adapter.close_position("mt5:111", 158.0, "exit_review", pm)

    assert result is closed_order
    assert safe_close_called == [111]
    pm.close_position.assert_called_once_with("mt5:111", 158.0, "exit_review")


def test_active_close_safe_close_failure_preserves_state(monkeypatch, tmp_path):
    """safe_close 失敗時は内部 state を変更せず None を返す (reconcile に委譲)。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    monkeypatch.setattr(adapter, "safe_close", lambda t, max_retries=3: False)
    pm = MagicMock()

    result = adapter.close_position("mt5:111", 158.0, "exit_review", pm)

    assert result is None
    pm.close_position.assert_not_called()


def test_active_close_paper_order_skips_mt5(monkeypatch, tmp_path):
    """非 mt5: order_id (paper UUID) は safe_close を呼ばず内部 close のみ。"""
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", lot_size_units=100_000, magic_number=12345,
        state_dir=tmp_path,
    )
    safe_close_calls: list[int] = []
    monkeypatch.setattr(
        adapter, "safe_close",
        lambda ticket, max_retries=3: safe_close_calls.append(ticket) or True,
    )
    pm = MagicMock()
    paper_order = Order.new("USDJPY=X", "buy", 159, 158, 160, 1000)
    pm.close_position.return_value = paper_order

    result = adapter.close_position(paper_order.order_id, 158.5, "exit_review", pm)

    assert result is paper_order
    assert safe_close_calls == []    # MT5 へは触らない
    pm.close_position.assert_called_once()
