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


def test_execute_signal_hold_returns_none(tmp_path):
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812", state_dir=tmp_path)
    sig = _make_signal(action="hold")
    pm = _make_pm()
    assert adapter.execute_signal(sig, pm) is None
    pm.open_position.assert_not_called()


def test_execute_signal_posts_order_and_records(monkeypatch, tmp_path):
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
        state_dir=tmp_path,
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


def test_execute_signal_with_api_key_sends_header(monkeypatch, tmp_path):
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
        state_dir=tmp_path,
    )
    adapter.execute_signal(_make_signal(action="buy"), pm)
    assert captured["headers"]["X-Bridge-Api-Key"] == "secret123"


def test_execute_signal_handles_bridge_error_gracefully(monkeypatch, caplog, tmp_path):
    def _post(*a, **kw):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr(httpx, "post", _post)

    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812", state_dir=tmp_path)
    sig = _make_signal(action="buy")
    with caplog.at_level("ERROR"):
        order = adapter.execute_signal(sig, pm)
    assert order is None
    assert any("bridge" in r.message.lower() for r in caplog.records)
    pm.open_position.assert_not_called()


def test_existing_position_returns_none(tmp_path):
    """scale-in disabled (default) で既存ポジがある場合はスキップする。"""
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://example:8812", state_dir=tmp_path)
    pm = _make_pm()
    existing = Order.new("USDJPY=X", "buy", 159, 158, 160, 1000)
    pm.get_open_positions_by_pair.return_value = [existing]
    sig = _make_signal()
    assert adapter.execute_signal(sig, pm) is None


# ── Phase 3b: HTTP status routing ──

def test_execute_signal_handles_422_margin_insufficient(monkeypatch, caplog, tmp_path):
    """422: 証拠金不足 → 警告ログ、order=None。"""
    class _Resp:
        is_success = False
        status_code = 422
        text = '{"detail":"insufficient margin"}'
        def json(self):
            return {"detail": "insufficient margin: required=6695 free=5200"}

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812", state_dir=tmp_path)
    with caplog.at_level("WARNING"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)

    assert order is None
    assert any("margin" in r.message.lower() for r in caplog.records)


def test_execute_signal_handles_423_soft_halt(monkeypatch, caplog, tmp_path):
    """423: soft halted → info ログ、order=None。"""
    class _Resp:
        is_success = False
        status_code = 423
        text = '{"detail":"soft halted"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812", state_dir=tmp_path)
    with caplog.at_level("INFO"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)

    assert order is None
    assert any("halted" in r.message.lower() for r in caplog.records)


def test_execute_signal_handles_409_order_rejected(monkeypatch, caplog, tmp_path):
    """409: bridge が retcode で reject した時の挙動。"""
    class _Resp:
        is_success = False
        status_code = 409
        text = "order rejected: retcode=10006"

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812", state_dir=tmp_path)
    with caplog.at_level("WARNING"):
        order = adapter.execute_signal(_make_signal(action="buy"), pm)
    assert order is None
    assert any("reject" in r.message.lower() for r in caplog.records)


def test_execute_signal_partial_fill_modifies_position_size(monkeypatch, tmp_path):
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
        state_dir=tmp_path,
    )
    sig = _make_signal(action="buy")    # signal.position_size = 1000.0

    order = adapter.execute_signal(sig, pm)

    assert order is not None
    # 1000 * (0.0075 / 0.01) = 750
    assert order.position_size == pytest.approx(750.0, rel=0.01)


def test_empty_bridge_url_raises():
    with pytest.raises(ValueError, match="bridge_url is required"):
        Mt5BridgeBrokerAdapter(bridge_url="")


# ── halt state チェック (Task 4) ──

from pathlib import Path as _Path  # noqa: E402


def _make_adapter_with_state(tmp_path: _Path) -> Mt5BridgeBrokerAdapter:
    return Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812",
        state_dir=tmp_path,
    )


def test_execute_signal_skips_when_finance_halted(tmp_path):
    """halt_state.is_halted=True なら execute_signal は bridge を叩かず None を返す。"""
    from src.persistence import halt_state
    halt_state.trigger_manual(tmp_path, reason="testing")
    adapter = _make_adapter_with_state(tmp_path)

    signal = _make_signal(action="buy")
    pm_stub = _make_pm()

    result = adapter.execute_signal(signal, pm_stub)

    assert result is None
    # bridge が呼ばれていないことを確認 (open_position も実行されていない)
    pm_stub.open_position.assert_not_called()


def test_execute_signal_proceeds_when_not_halted(tmp_path, monkeypatch):
    """halt_state.is_halted=False なら通常フローへ進む (PreExec まで到達する)。"""
    adapter = _make_adapter_with_state(tmp_path)

    # PreExec を skip 結果で短絡させる (= bridge HTTP までは行かないが、halt
    # チェック以降のロジックが実行されたことを確認できる)
    monkeypatch.setattr(
        "src.trading.mt5_bridge_broker.evaluate_pre_execution_checks",
        lambda **kw: MagicMock(status="skip", reason="test pre-exec skip"),
    )

    signal = _make_signal(action="buy")

    result = adapter.execute_signal(signal, _make_pm())

    # halt チェック通過 → PreExec で skip 判定 → None を返す
    assert result is None


def test_auto_soft_halt_updates_finance_state_when_bridge_post_fails(
    tmp_path, monkeypatch
):
    """_auto_soft_halt は finance state を確定させ、bridge POST 失敗を許容する。"""
    import httpx
    from src.persistence import halt_state

    adapter = _make_adapter_with_state(tmp_path)

    # bridge POST は接続エラー
    def _post(*a, **kw):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr("httpx.post", _post)

    adapter._auto_soft_halt("3 consecutive rejects", triggered_by="order_reject")

    state = halt_state.read(tmp_path)
    assert state.soft_halted is True
    assert state.auto_triggered is True
    assert state.triggered_by == "order_reject"


def test_execute_signal_mirrors_423_to_finance_halt(monkeypatch, tmp_path):
    """bridge が /order で 423 を返したら finance halt.json にも soft halt を立てる。"""
    from src.persistence import halt_state

    class _Resp:
        is_success = False
        status_code = 423
        text = '{"detail":"soft halted"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(bridge_url="http://x:8812", state_dir=tmp_path)

    order = adapter.execute_signal(_make_signal(action="buy"), pm)

    assert order is None
    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "bridge_423"
    assert "423" in s.reason


def test_execute_signal_mirrors_423_fires_notifier_on_first_observation(
    monkeypatch, tmp_path,
):
    """初回 423 観測時のみ Discord 通知 (changed=True 1 回)。

    2 回目は execute_signal 冒頭の is_halted で早期 return されるため、
    notifier.send_embed の呼び出しは累計 1 回のままになる (idempotent)。
    """
    class _Resp:
        is_success = False
        status_code = 423
        text = '{"detail":"soft halted"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())

    notifier = MagicMock()
    notifier.send_embed = MagicMock()

    # asyncio.run を no-op にして coroutine を消費する。
    # 実装側は asyncio.run(notifier.send_embed(...)) と呼ぶため、
    # send_embed 自体は coroutine を返さない MagicMock でも、
    # ここで coroutine を捨てれば送信そのものが 1 回行われたことを
    # send_embed.call_count で計測できる。
    def _consume(coro):
        try:
            coro.send(None)
        except (StopIteration, AttributeError):
            pass
    monkeypatch.setattr("asyncio.run", lambda coro: _consume(coro))

    pm = _make_pm()
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", state_dir=tmp_path, notifier=notifier,
    )

    # 1 回目: 423 観測 → halt.json mirror + Discord 1 通
    order1 = adapter.execute_signal(_make_signal(action="buy"), pm)
    assert order1 is None
    assert notifier.send_embed.call_count == 1

    # 2 回目: 既に halted なので冒頭 is_halted で早期 return → Discord は増えない
    order2 = adapter.execute_signal(_make_signal(action="buy"), pm)
    assert order2 is None
    assert notifier.send_embed.call_count == 1


def test_trigger_hard_halt_fallbacks_to_finance_soft_halt_on_bridge_failure(
    monkeypatch, tmp_path,
):
    """bridge /admin/halt mode=hard が失敗したら finance に soft halt を立てる fail-safe。"""
    import httpx
    from src.persistence import halt_state

    def _raise(*a, **kw):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr(httpx, "post", _raise)

    notifier = MagicMock()

    # asyncio.run の coroutine を消費するモック (notifier.send_embed の呼出回数だけ確認)
    async def _consume(coro):
        try:
            coro.send(None)
        except (StopIteration, AttributeError):
            pass
    monkeypatch.setattr("asyncio.run", lambda coro: _consume(coro))

    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", state_dir=tmp_path, notifier=notifier,
    )
    adapter._trigger_hard_halt("orphan position detected")

    # finance soft halt が立つ
    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "hard_halt_delivery_failed"
    assert "orphan" in s.reason

    # Discord に「hard halt 未達」通知が出る
    assert notifier.send_embed.call_count >= 1


def test_trigger_hard_halt_succeeds_when_bridge_reachable(monkeypatch, tmp_path):
    """bridge POST 成功時は finance soft halt は立たない (hard halt 経路は bridge 側のみ)。"""
    from src.persistence import halt_state

    class _Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())

    notifier = MagicMock()
    adapter = Mt5BridgeBrokerAdapter(
        bridge_url="http://x:8812", state_dir=tmp_path, notifier=notifier,
    )
    adapter._trigger_hard_halt("test reason")

    # bridge POST 成功時は finance soft halt は立たない
    s = halt_state.read(tmp_path)
    assert s.soft_halted is False


# ── update_remote_sl (Task 4) ──

def _make_update_sl_adapter(bridge_url="http://test"):
    from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
    adapter = Mt5BridgeBrokerAdapter.__new__(Mt5BridgeBrokerAdapter)
    adapter._url = bridge_url
    adapter._timeout = 2.0
    adapter._api_key = ""
    # update_remote_sl uses _url / _timeout / _api_key / _headers() / class-level
    # _modify_404_logged. Above 3 attrs sufficient.
    return adapter


def test_update_remote_sl_success_returns_true() -> None:
    from unittest.mock import patch, MagicMock
    adapter = _make_update_sl_adapter()
    with patch("src.trading.mt5_bridge_broker.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp
        ok = adapter.update_remote_sl("mt5:12345", 1.2345)
        assert ok is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "/positions/12345/modify" in call_args.args[0]
        # Body は sl のみ送信 (tp キーは含めない = TP 据置)
        assert call_args.kwargs["json"] == {"sl": 1.2345}


def test_update_remote_sl_http_error_returns_false(caplog) -> None:
    import logging
    from unittest.mock import patch
    caplog.set_level(logging.WARNING, logger="src.trading.mt5_bridge_broker")
    adapter = _make_update_sl_adapter()
    with patch("src.trading.mt5_bridge_broker.httpx.post",
               side_effect=httpx.HTTPError("connection refused")):
        ok = adapter.update_remote_sl("mt5:12345", 1.2345)
        assert ok is False
        assert any("modify failed" in r.message.lower()
                   for r in caplog.records)


def test_update_remote_sl_404_logged_once_per_ticket(caplog) -> None:
    """bridge 未実装の 404 は ticket あたり WARN 1 回のみ (de-duplicate)。"""
    import logging
    from unittest.mock import patch, MagicMock
    caplog.set_level(logging.WARNING, logger="src.trading.mt5_bridge_broker")
    # 別 ticket でテストするため、class-level set をクリア
    from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
    Mt5BridgeBrokerAdapter._modify_404_logged.clear()

    adapter = _make_update_sl_adapter()
    resp = MagicMock()
    resp.status_code = 404
    err = httpx.HTTPStatusError("not found", request=MagicMock(), response=resp)
    resp.raise_for_status.side_effect = err
    with patch("src.trading.mt5_bridge_broker.httpx.post", return_value=resp):
        adapter.update_remote_sl("mt5:99999", 1.2345)
        adapter.update_remote_sl("mt5:99999", 1.2350)
        adapter.update_remote_sl("mt5:99999", 1.2360)
    warn_404 = [r for r in caplog.records if "capability disabled" in r.message.lower()
                                          or "404" in r.message]
    assert len(warn_404) == 1


def test_update_remote_sl_non_mt5_order_id_noop() -> None:
    """paper UUID 由来の order_id は HTTP 呼ばれず True を返す。"""
    from unittest.mock import patch
    adapter = _make_update_sl_adapter()
    with patch("src.trading.mt5_bridge_broker.httpx.post") as mock_post:
        ok = adapter.update_remote_sl("paper-abc-123", 1.2345)
        assert ok is True
        mock_post.assert_not_called()


def test_default_update_remote_sl_returns_true() -> None:
    """default 実装は no-op で True を返す (paper / shadow / live OANDA)。"""
    from src.trading.paper_broker import PaperBrokerAdapter
    adapter = PaperBrokerAdapter.__new__(PaperBrokerAdapter)
    ok = adapter.update_remote_sl("any-id", 1.2345)
    assert ok is True
