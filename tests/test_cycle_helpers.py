"""`src/cycles/_helpers.py` の温存ヘルパーの直接テスト。

Task 8 で `tests/test_trading_cycle_helpers.py` を全削除したため、温存する
`_get_price` / `_summarize_pair` を直接カバーするテストが無くなった。
plan Task 8「共有ヘルパのテスト空白を判断する」の選択肢 1 (最小テストを新設) を採る。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.cycles._helpers import _get_price, _summarize_pair


# ── _get_price ────────────────────────────────────────────────────────


def test_get_price_uses_price_provider_when_given():
    provider = MagicMock()
    provider.get_current_price.return_value = SimpleNamespace(price=151.25)

    assert _get_price("USDJPY=X", provider) == 151.25
    provider.get_current_price.assert_called_once_with("USDJPY=X")


def test_get_price_falls_back_to_direct_fetch_when_provider_is_none(monkeypatch):
    """provider 未指定なら fetch_current_price へフォールバックする。"""
    called: dict = {}

    def _fake_fetch(symbol):
        called["symbol"] = symbol
        return SimpleNamespace(price=1.0855)

    monkeypatch.setattr("src.cycles._helpers.fetch_current_price", _fake_fetch)

    assert _get_price("EURUSD=X", None) == 1.0855
    assert called["symbol"] == "EURUSD=X"


# ── _summarize_pair ───────────────────────────────────────────────────


def _config():
    cfg = MagicMock()
    cfg.rag.analysis_lookback_hours = 8
    cfg.trading.risk_per_trade = 0.01
    cfg.trading.signal_confidence_threshold = 0.6
    cfg.trading.news_weight = 0.5
    cfg.trading.price_weight = 0.5
    cfg.trading.signal_deadband = 0.05
    cfg.trading.min_lot_size = 1000
    cfg.trading.lot_unit = 1000
    cfg.trading.min_rr_ratio = 1.5
    return cfg


def _pair_cfg():
    return SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY")


def test_summarize_pair_returns_none_when_no_snapshot():
    """保存済みスナップショットが無ければ None (新規取得はしない)。"""
    analysis_store = MagicMock()
    analysis_store.aggregate.return_value = None

    result = asyncio.run(_summarize_pair(
        _pair_cfg(), _config(), MagicMock(), MagicMock(), analysis_store,
    ))

    assert result is None


def test_summarize_pair_combines_signals_from_stored_data(monkeypatch):
    """スナップショットがあれば combine_signals の結果をそのまま返す。"""
    sentinel = object()
    captured: dict = {}

    monkeypatch.setattr(
        "src.cycles._helpers.aggregate_news_sentiment",
        lambda *a, **kw: "news",
    )
    monkeypatch.setattr(
        "src.cycles._helpers._get_price",
        lambda *a, **kw: 150.0,
    )

    def _fake_combine(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("src.cycles._helpers.combine_signals", _fake_combine)

    analysis_store = MagicMock()
    analysis_store.aggregate.return_value = "price"
    position_mgr = MagicMock()
    position_mgr.get_account_state.return_value = SimpleNamespace(balance=100_000.0)

    result = asyncio.run(_summarize_pair(
        _pair_cfg(), _config(), position_mgr, MagicMock(), analysis_store,
    ))

    assert result is sentinel
    assert captured["current_price"] == 150.0
    assert captured["account_balance"] == 100_000.0


def test_summarize_pair_returns_none_on_exception():
    """内部例外は握り潰して None (サイクル全体を落とさない)。"""
    analysis_store = MagicMock()
    analysis_store.aggregate.side_effect = RuntimeError("boom")

    result = asyncio.run(_summarize_pair(
        _pair_cfg(), _config(), MagicMock(), MagicMock(), analysis_store,
    ))

    assert result is None
