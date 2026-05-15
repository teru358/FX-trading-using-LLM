"""exit_check_cycle reviewer phase の close ガードテスト。

2026-05-14 incident 再発防止:
- live mode + price source != "mt5" → close skip (TwelveData 等の fallback 価格で
  L1/L2/L3 close を発火させない)
- soft halt 中 + close_reason != "timeout" → close skip
- 上記いずれも reviewer の評価 (signal_combiner 等) と SL/TP reconciliation は継続
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.cycles.exit_check import exit_check_cycle
from src.data.price_fetcher import CurrentPrice
from src.persistence import halt_state
from src.persistence.balance_snapshot import BalanceSnapshot
from src.persistence.balance_snapshot import write as write_balance

_FIXED_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


# ─── テスト用 fixture ─────────────────────────────────────

@pytest.fixture
def _config(tmp_path):
    cfg = MagicMock()
    cfg.mode = "live"
    cfg.live_broker = "mt5"
    cfg.paper_provider = "twelvedata"
    cfg.state_dir = tmp_path
    cfg.tradeable_instruments = []
    cfg.providers.mt5 = MagicMock(
        bridge_url="http://x:8812",
        api_key="",
        lot_size_units=100_000,
        magic_number=12345,
        order_request_timeout_seconds=10.0,
        shadow_log_path=str(tmp_path / "shadow.jsonl"),
        shadow_observer_state_dir=str(tmp_path / "shadow_state"),
    )
    # PositionReviewer 関連 (定数値)
    cfg.trading.position_review_enabled = True
    cfg.trading.reversal_confidence_min = 0.7
    cfg.trading.reversal_score_threshold = 0.25
    cfg.trading.reversal_min_holding_minutes = 240
    cfg.trading.max_holding_days = 14
    cfg.trading.timeout_min_progress_pct = 0.2
    cfg.trading.profit_lock_min_progress_pct = 0.3
    cfg.trading.profit_lock_score_floor = 0.15
    cfg.notifier.enabled = False
    cfg.notifier.notify_on_order_close = False

    write_balance(
        tmp_path,
        BalanceSnapshot(
            balance=50_000.0, deposit=50_000.0, peak_balance=50_000.0,
            source="paper",
            fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    return cfg


def _make_position(pair: str, order_id: str, entry: float, sl: float, tp: float):
    """exit_check に渡すための Order 互換 mock。"""
    pos = MagicMock()
    pos.pair = pair
    pos.order_id = order_id
    pos.entry_price = entry
    pos.stop_loss = sl
    pos.take_profit = tp
    pos.direction = "buy"
    pos.position_size = 10_000.0
    pos.opened_at = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    return pos


# ─── Guard 1: price source != "mt5" → live close skip ─────

def test_live_mode_skips_close_when_price_source_is_not_mt5(_config, monkeypatch):
    """live mode で price.source = 'twelvedata' の pair は reviewer close 不可。"""
    from src.cycles import exit_check as ec_module

    position = _make_position("USDJPY=X", "mt5:1001", 150.0, 149.0, 152.0)
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = MagicMock(
        open_positions=[position], balance=50_000.0,
    )

    # price_provider: TwelveData フォールバック (source="twelvedata")
    price_provider = MagicMock()
    price_provider.get_current_price.return_value = CurrentPrice(
        price=151.5,
        timestamp=datetime.now(tz=timezone.utc),
        is_market_open=True,
        source="twelvedata",   # ← MT5 不通フォールバック
    )

    # reviewer が profit_lock decision を返すよう mock
    decision = MagicMock(
        order_id="mt5:1001",
        pair="USDJPY=X",
        close_reason="profit_lock",
        detail="signal weakened",
    )
    monkeypatch.setattr(
        ec_module, "review_open_positions",
        lambda **kwargs: [decision],
    )
    monkeypatch.setattr(
        ec_module, "_summarize_pair",
        lambda *a, **kw: asyncio.sleep(0, result=MagicMock(pair="USDJPY=X")),
    )

    # broker.close_position が呼ばれないことを確認するため監視
    close_calls: list = []
    broker = MagicMock()
    broker.check_and_close_positions.return_value = []
    broker.close_position.side_effect = lambda *a, **kw: close_calls.append(a) or None
    monkeypatch.setattr(ec_module, "create_broker", lambda *a, **kw: broker)

    monkeypatch.setattr(ec_module, "is_market_open", lambda _now: True)
    monkeypatch.setattr(ec_module, "local_now", lambda _cfg: _FIXED_NOW)

    asyncio.run(exit_check_cycle(
        _config, position_mgr,
        store=MagicMock(), analysis_store=MagicMock(),
        price_provider=price_provider,
    ))

    assert close_calls == [], (
        "live mode で price.source='twelvedata' のとき reviewer close は "
        "skip されるべきだが broker.close_position が呼ばれた"
    )


def test_live_mode_executes_close_when_price_source_is_mt5(_config, monkeypatch):
    """live mode で price.source = 'mt5' の pair は reviewer close 実行可。"""
    from src.cycles import exit_check as ec_module

    position = _make_position("USDJPY=X", "mt5:1001", 150.0, 149.0, 152.0)
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = MagicMock(
        open_positions=[position], balance=50_000.0,
    )

    price_provider = MagicMock()
    price_provider.get_current_price.return_value = CurrentPrice(
        price=151.5,
        timestamp=datetime.now(tz=timezone.utc),
        is_market_open=True,
        source="mt5",   # ← MT5 価格取得成功
    )

    decision = MagicMock(
        order_id="mt5:1001",
        pair="USDJPY=X",
        close_reason="profit_lock",
        detail="signal weakened",
    )
    monkeypatch.setattr(
        ec_module, "review_open_positions",
        lambda **kwargs: [decision],
    )
    monkeypatch.setattr(
        ec_module, "_summarize_pair",
        lambda *a, **kw: asyncio.sleep(0, result=MagicMock(pair="USDJPY=X")),
    )

    close_calls: list = []
    broker = MagicMock()
    broker.check_and_close_positions.return_value = []
    closed_order = MagicMock(pair="USDJPY=X", direction="buy",
                             entry_price=150.0, realized_pnl=15.0)
    broker.close_position.side_effect = (
        lambda *a, **kw: close_calls.append(a) or closed_order
    )
    monkeypatch.setattr(ec_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(ec_module, "is_market_open", lambda _now: True)
    monkeypatch.setattr(ec_module, "local_now", lambda _cfg: _FIXED_NOW)

    asyncio.run(exit_check_cycle(
        _config, position_mgr,
        store=MagicMock(), analysis_store=MagicMock(),
        price_provider=price_provider,
    ))

    assert len(close_calls) == 1, (
        "live mode + price.source='mt5' なら reviewer close は実行されるべき"
    )


def test_paper_mode_executes_close_regardless_of_price_source(_config, monkeypatch):
    """paper / live_test mode では source 問わず close 実行 (Guard 1 は live 限定)。"""
    from src.cycles import exit_check as ec_module

    _config.mode = "live_test"   # paper primary

    position = _make_position("USDJPY=X", "abc-uuid-not-mt5", 150.0, 149.0, 152.0)
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = MagicMock(
        open_positions=[position], balance=50_000.0,
    )

    price_provider = MagicMock()
    price_provider.get_current_price.return_value = CurrentPrice(
        price=151.5, timestamp=datetime.now(tz=timezone.utc),
        is_market_open=True, source="twelvedata",
    )

    decision = MagicMock(
        order_id="abc-uuid-not-mt5",
        pair="USDJPY=X",
        close_reason="profit_lock",
        detail="signal weakened",
    )
    monkeypatch.setattr(ec_module, "review_open_positions",
                        lambda **kwargs: [decision])
    monkeypatch.setattr(
        ec_module, "_summarize_pair",
        lambda *a, **kw: asyncio.sleep(0, result=MagicMock(pair="USDJPY=X")),
    )

    close_calls: list = []
    broker = MagicMock()
    broker.check_and_close_positions.return_value = []
    closed_order = MagicMock(pair="USDJPY=X", direction="buy",
                             entry_price=150.0, realized_pnl=15.0)
    broker.close_position.side_effect = (
        lambda *a, **kw: close_calls.append(a) or closed_order
    )
    monkeypatch.setattr(ec_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(ec_module, "is_market_open", lambda _now: True)
    monkeypatch.setattr(ec_module, "local_now", lambda _cfg: _FIXED_NOW)

    asyncio.run(exit_check_cycle(
        _config, position_mgr,
        store=MagicMock(), analysis_store=MagicMock(),
        price_provider=price_provider,
    ))

    assert len(close_calls) == 1, (
        "live_test mode は Guard 1 対象外で close 実行されるべき"
    )


# ─── Guard 2: soft halt 中は timeout 以外 skip ─────────────

def test_halt_blocks_non_timeout_close(_config, monkeypatch):
    """soft halt 中は profit_lock / reversal の close は skip される。"""
    from src.cycles import exit_check as ec_module

    halt_state.trigger_manual(_config.state_dir, reason="test halt")

    position = _make_position("USDJPY=X", "mt5:1001", 150.0, 149.0, 152.0)
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = MagicMock(
        open_positions=[position], balance=50_000.0,
    )

    price_provider = MagicMock()
    price_provider.get_current_price.return_value = CurrentPrice(
        price=151.5, timestamp=datetime.now(tz=timezone.utc),
        is_market_open=True, source="mt5",
    )

    decision = MagicMock(
        order_id="mt5:1001",
        pair="USDJPY=X",
        close_reason="profit_lock",
        detail="signal weakened",
    )
    monkeypatch.setattr(ec_module, "review_open_positions",
                        lambda **kwargs: [decision])
    monkeypatch.setattr(
        ec_module, "_summarize_pair",
        lambda *a, **kw: asyncio.sleep(0, result=MagicMock(pair="USDJPY=X")),
    )

    close_calls: list = []
    broker = MagicMock()
    broker.check_and_close_positions.return_value = []
    broker.close_position.side_effect = lambda *a, **kw: close_calls.append(a) or None
    monkeypatch.setattr(ec_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(ec_module, "is_market_open", lambda _now: True)
    monkeypatch.setattr(ec_module, "local_now", lambda _cfg: _FIXED_NOW)

    asyncio.run(exit_check_cycle(
        _config, position_mgr,
        store=MagicMock(), analysis_store=MagicMock(),
        price_provider=price_provider,
    ))

    assert close_calls == [], (
        "soft halt 中の profit_lock close は skip されるべき"
    )


def test_halt_allows_timeout_close(_config, monkeypatch):
    """soft halt 中でも close_reason='timeout' は実行される (最大保有期間超過は halt と無関係)。"""
    from src.cycles import exit_check as ec_module

    halt_state.trigger_manual(_config.state_dir, reason="test halt")

    position = _make_position("USDJPY=X", "mt5:1001", 150.0, 149.0, 152.0)
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = MagicMock(
        open_positions=[position], balance=50_000.0,
    )

    price_provider = MagicMock()
    price_provider.get_current_price.return_value = CurrentPrice(
        price=151.5, timestamp=datetime.now(tz=timezone.utc),
        is_market_open=True, source="mt5",
    )

    decision = MagicMock(
        order_id="mt5:1001",
        pair="USDJPY=X",
        close_reason="timeout",
        detail="14 days exceeded",
    )
    monkeypatch.setattr(ec_module, "review_open_positions",
                        lambda **kwargs: [decision])
    monkeypatch.setattr(
        ec_module, "_summarize_pair",
        lambda *a, **kw: asyncio.sleep(0, result=MagicMock(pair="USDJPY=X")),
    )

    close_calls: list = []
    broker = MagicMock()
    broker.check_and_close_positions.return_value = []
    closed_order = MagicMock(pair="USDJPY=X", direction="buy",
                             entry_price=150.0, realized_pnl=-10.0)
    broker.close_position.side_effect = (
        lambda *a, **kw: close_calls.append(a) or closed_order
    )
    monkeypatch.setattr(ec_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(ec_module, "is_market_open", lambda _now: True)
    monkeypatch.setattr(ec_module, "local_now", lambda _cfg: _FIXED_NOW)

    asyncio.run(exit_check_cycle(
        _config, position_mgr,
        store=MagicMock(), analysis_store=MagicMock(),
        price_provider=price_provider,
    ))

    assert len(close_calls) == 1, (
        "soft halt 中でも timeout close は実行されるべき"
    )
