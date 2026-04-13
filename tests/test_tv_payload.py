"""tv_payload.build_tv_pine のテスト。

DB/CDP に触れず、モックの analysis_store と position_mgr を使う。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.tradingview.tv_payload import build_tv_pine


def _fake_config():
    """最小限の AppConfig モック。"""
    tradeable = [
        SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY"),
        SimpleNamespace(symbol="EURUSD=X", display_name="EUR/USD"),
    ]
    return SimpleNamespace(
        tradeable_instruments=tradeable,
        watch_only_instruments=[],
    )


def _fake_snapshot(**overrides):
    base = dict(
        direction_bias="long",
        confidence=0.75,
        bias_score=0.30,
        reasoning_summary="Test reason",
        key_support=None,
        key_resistance=None,
        recent_highs=[],
        recent_lows=[],
        chart_patterns=[],
        trend_direction="uptrend",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_order(**overrides):
    base = dict(
        order_id="test-1",
        pair="USDJPY=X",
        direction="buy",
        entry_price=158.85,
        stop_loss=157.26,
        take_profit=162.03,
        position_size=10000.0,
        opened_at=datetime(2026, 4, 10, 12, 0, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_tv_pine_signals_only():
    """ポジションなし、シグナルのみ → Pine Script に signal ブロックあり、positions ブロックはループ空。"""
    cfg = _fake_config()
    analysis_store = MagicMock()
    analysis_store.get_recent_snapshots.return_value = [_fake_snapshot()]
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = SimpleNamespace(open_positions=[])

    script = build_tv_pine(cfg, analysis_store, position_mgr)
    assert script is not None
    assert "//@version=6" in script
    assert "USDJPY" in script


def test_build_tv_pine_with_positions():
    """オープンポジションありのケース → positions ブロックに entry 価格が含まれる。"""
    cfg = _fake_config()
    analysis_store = MagicMock()
    analysis_store.get_recent_snapshots.return_value = [_fake_snapshot()]
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = SimpleNamespace(
        open_positions=[_fake_order()]
    )

    script = build_tv_pine(cfg, analysis_store, position_mgr)
    assert script is not None
    assert "158.85" in script
    assert "157.26" in script
    assert "162.03" in script
    assert "▲" in script  # buy


def test_build_tv_pine_empty_returns_cleared_script():
    """シグナルもポジションも空なら、空の Pine Script を返して古い線をクリアできる。"""
    cfg = _fake_config()
    analysis_store = MagicMock()
    analysis_store.get_recent_snapshots.return_value = []  # スナップショットなし
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = SimpleNamespace(open_positions=[])

    script = build_tv_pine(cfg, analysis_store, position_mgr)
    # 空でも indicator ヘッダは必ず含まれる (古い indicator を上書きクリアする目的)
    assert script is not None
    assert "indicator(" in script
    assert "0 signal(s), 0 position(s)" in script


def test_build_tv_pine_opened_at_converted_to_ms():
    """opened_at (datetime) が UNIX ミリ秒に変換される。"""
    cfg = _fake_config()
    analysis_store = MagicMock()
    analysis_store.get_recent_snapshots.return_value = []
    position_mgr = MagicMock()
    order = _fake_order(opened_at=datetime(2026, 4, 10, 12, 0, 0))
    position_mgr.get_account_state.return_value = SimpleNamespace(
        open_positions=[order]
    )

    script = build_tv_pine(cfg, analysis_store, position_mgr)
    assert script is not None
    import re
    assert re.search(r"xloc=xloc\.bar_time", script)
    # 13桁の整数 (ミリ秒) が出力されている
    assert re.search(r"\b\d{13}\b", script)
