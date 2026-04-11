"""trading_cycle.py のヘルパー関数ユニットテスト。

_fetch_and_compute_atr / _apply_atr_sltp_to_signal が fetch_ohlcv / _get_ohlcv に
渡す period 引数が正しい str 形式 ("90d") であることを保証する。

Regression guard: 以前 config.trading.lookback_days(int) をそのまま渡しており、
fetch_ohlcv の _parse_period_days が AttributeError で落ちて ATR が常に None に
なっていた。本テストはその退行を検知する。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data.price_fetcher import PriceData


def _make_price_data(symbol: str = "USDJPY=X", bars: int = 20) -> PriceData:
    """pandas_ta.atr(14) が計算できる最低限の OHLCV を返す。"""
    idx = pd.date_range("2026-04-01", periods=bars, freq="1h")
    df = pd.DataFrame(
        {
            "Open":   [150.0 + i * 0.1 for i in range(bars)],
            "High":   [150.5 + i * 0.1 for i in range(bars)],
            "Low":    [149.5 + i * 0.1 for i in range(bars)],
            "Close":  [150.2 + i * 0.1 for i in range(bars)],
            "Volume": [1000] * bars,
        },
        index=idx,
    )
    return PriceData(
        symbol=symbol,
        df=df,
        current_price=float(df["Close"].iloc[-1]),
        fetched_at=datetime.now(),
    )


# ── _fetch_and_compute_atr ────────────────────────────────────


def test_fetch_and_compute_atr_passes_string_period(monkeypatch):
    """_fetch_and_compute_atr は fetch_ohlcv に str 形式の period を渡すこと。"""
    from src.config import load_config
    from src.trading_cycle import _fetch_and_compute_atr

    captured: dict = {}

    def fake_fetch(symbol, period, interval, price_store=None):
        captured["period"] = period
        return _make_price_data(symbol)

    monkeypatch.setattr("src.data.price_fetcher.fetch_ohlcv", fake_fetch)

    config = load_config()

    class _DummyStore:
        pass

    atr = _fetch_and_compute_atr("USDJPY=X", config, _DummyStore())

    # period が str として渡っていること (int だと fetch_ohlcv 内部で AttributeError)
    assert isinstance(captured.get("period"), str), (
        f"period must be str, got {type(captured.get('period')).__name__}"
    )
    assert captured["period"].endswith("d"), (
        f"period must be like '90d', got {captured['period']!r}"
    )

    # ATR が計算されて戻ってくること (bug 時は None)
    assert atr is not None, "ATR should not be None when valid OHLCV is returned"
    assert atr > 0


def test_fetch_and_compute_atr_none_when_no_store():
    from src.config import load_config
    from src.trading_cycle import _fetch_and_compute_atr

    config = load_config()
    assert _fetch_and_compute_atr("USDJPY=X", config, None) is None


# ── _apply_atr_sltp_to_signal ─────────────────────────────────


def test_apply_atr_sltp_to_signal_uses_string_period(monkeypatch):
    """_apply_atr_sltp_to_signal も同様に str 形式 period を要求する。"""
    from src.config import load_config
    from src.persistence.adaptive_params_store import AdaptiveParamsStore
    from src.trading_cycle import _apply_atr_sltp_to_signal

    captured: dict = {}

    def fake_fetch(symbol, period, interval, price_store=None):
        captured["period"] = period
        return _make_price_data(symbol)

    # _get_ohlcv (helpers) は module-top で `from src.data.price_fetcher import
    # fetch_ohlcv` を local binding しているため、そちらを直接 patch する必要がある。
    monkeypatch.setattr("src.cycles._helpers.fetch_ohlcv", fake_fetch)

    config = load_config()

    # 最小限の signal / position_mgr / adaptive_store モック
    class _Price:
        entry_zone = (150.0, 150.5)
        key_support = 149.0
        key_resistance = 151.0

    class _Signal:
        pair = "USDJPY=X"
        action = "buy"
        entry_price = 150.3
        stop_loss = 149.0
        take_profit = 152.0
        position_size = 1000.0
        combined_score = 0.5
        confidence = 0.8
        price = _Price()

    class _Account:
        balance = 10000.0

    class _PM:
        def get_account_state(self):
            return _Account()

    adaptive_store = AdaptiveParamsStore(
        state_dir=config.state_dir,
        defaults={
            "sl_atr_mult": config.trading.sl_atr_mult_default,
            "tp_atr_mult": config.trading.tp_atr_mult_default,
        },
        limits={
            "sl_atr_mult_min": config.trading.sl_atr_mult_min,
            "sl_atr_mult_max": config.trading.sl_atr_mult_max,
            "tp_atr_mult_min": config.trading.tp_atr_mult_min,
            "tp_atr_mult_max": config.trading.tp_atr_mult_max,
        },
    )

    class _DummyStore:
        pass

    result = _apply_atr_sltp_to_signal(
        _Signal(), config, _PM(), _DummyStore(), adaptive_store,
        price_provider=None,
    )

    assert isinstance(captured.get("period"), str), (
        f"period must be str, got {type(captured.get('period')).__name__}"
    )
    assert captured["period"].endswith("d")
    # ATR が valid なら calculate_sl_tp が SLTPResult を返すはず
    assert result is not None, "SLTPResult should not be None when valid OHLCV is returned"


# ── _adjust_signal_with_rag ───────────────────────────────────


def _make_signal(pair: str = "USDJPY=X", action: str = "buy", score: float = 0.30, confidence: float = 0.70):
    """最小限の TradeSignal 相当モック。"""
    class _Sig:
        pass
    s = _Sig()
    s.pair = pair
    s.action = action
    s.combined_score = score
    s.confidence = confidence
    s.detail_reason = f"{pair} analysis"
    return s


class _FakeDirectional:
    """store.directional のフェイク。test が望む hit 群を返す。"""

    def __init__(self, same_hits=None, opposite_hits=None):
        self.same_hits = same_hits or []
        self.opposite_hits = opposite_hits or []
        self.calls: list[dict] = []

    def query(self, query_embedding, direction, top_k, phase_filter=None):
        self.calls.append({"direction": direction, "top_k": top_k, "phase_filter": phase_filter})
        # score 正 → same="bullish", opposite="bearish" / 負の場合は逆
        if direction == "bullish":
            return self.same_hits if not self._score_is_negative else self.opposite_hits
        return self.opposite_hits if not self._score_is_negative else self.same_hits

    # score 符号は test がセット
    _score_is_negative = False


class _FakeStoreWithDirectional:
    def __init__(self, directional: _FakeDirectional):
        self.directional = directional


async def _fake_embed(text: str) -> list[float]:
    return [0.1] * 768


@pytest.mark.asyncio
async def test_adjust_signal_with_rag_disabled_is_noop():
    """rag_cfg.enabled=False なら補正しない。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    sig = _make_signal(score=0.30)
    orig_score, orig_action = sig.combined_score, sig.action

    directional = _FakeDirectional()
    cfg = RagAdjustmentConfig(enabled=False)
    await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(directional), _fake_embed, deadband=0.15,
    )

    assert sig.combined_score == orig_score
    assert sig.action == orig_action
    assert directional.calls == [], "disabled 時は directional.query を呼ばない"


@pytest.mark.asyncio
async def test_adjust_signal_with_rag_hold_action_is_noop():
    """sig.action == 'hold' なら補正しない。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    sig = _make_signal(action="hold", score=0.05)
    directional = _FakeDirectional()
    cfg = RagAdjustmentConfig(enabled=True)

    await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(directional), _fake_embed, deadband=0.15,
    )

    assert sig.action == "hold"
    assert directional.calls == []


@pytest.mark.asyncio
async def test_adjust_signal_with_rag_no_hits_leaves_score_unchanged():
    """hit が空なら adjustment は 0 でスコアは据え置き。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    sig = _make_signal(score=0.30)
    directional = _FakeDirectional(same_hits=[], opposite_hits=[])
    cfg = RagAdjustmentConfig(enabled=True, min_hits=2)

    await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(directional), _fake_embed, deadband=0.15,
    )

    assert sig.combined_score == 0.30
    assert sig.action == "buy"
    # directional.query は 2 回呼ばれる (same / opposite)
    assert len(directional.calls) == 2


@pytest.mark.asyncio
async def test_adjust_signal_with_rag_embed_error_is_swallowed():
    """embed 失敗時も例外を投げずスコアは据え置き。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    sig = _make_signal(score=0.30)

    async def _broken_embed(text: str):
        raise RuntimeError("ollama down")

    cfg = RagAdjustmentConfig(enabled=True)
    await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(_FakeDirectional()), _broken_embed, deadband=0.15,
    )

    assert sig.combined_score == 0.30
    assert sig.action == "buy"


# ── _phase_close_sl_tp ────────────────────────────────────────


class _FakeAccount:
    def __init__(self, open_positions=None, balance: float = 10000.0):
        self.open_positions = open_positions or []
        self.balance = balance


class _FakePositionMgr:
    def __init__(self, account: _FakeAccount):
        self._account = account
        self.close_calls: list = []

    def get_account_state(self):
        return self._account


class _FakePosition:
    def __init__(self, pair, direction="buy", entry=150.0):
        self.pair = pair
        self.direction = direction
        self.entry_price = entry
        self.close_price = None
        self.realized_pnl = None
        self.close_reason = None


class _FakeClosedOrder:
    def __init__(self, pair, realized=100.0, reason="take_profit"):
        self.pair = pair
        self.direction = "buy"
        self.entry_price = 150.0
        self.close_price = 150.5
        self.realized_pnl = realized
        self.close_reason = reason


class _FakeBroker:
    def __init__(self, to_close: list):
        self.to_close = to_close
        self.check_called_with: dict = {}

    def check_and_close_positions(self, open_positions, current_prices, position_mgr):
        self.check_called_with = {
            "open_positions": open_positions,
            "current_prices": current_prices,
        }
        return self.to_close


class _FakeNotifier:
    def __init__(self):
        self.closed_events: list = []
        self.opened_events: list = []
        self.skipped_events: list = []

    async def notify_order_closed(self, event):
        self.closed_events.append(event)

    async def notify_order_opened(self, event):
        self.opened_events.append(event)

    async def notify_signal_skipped(self, event):
        self.skipped_events.append(event)


class _FakeNotifierCfg:
    notify_on_order_close = True
    notify_on_order_open = True
    notify_on_signal_skipped = True


class _FakeTradingCfg:
    pass


class _FakeConfigForClose:
    notifier = _FakeNotifierCfg()


@pytest.mark.asyncio
async def test_phase_close_sl_tp_no_open_positions_returns_empty():
    from src.trading_cycle import _phase_close_sl_tp

    pm = _FakePositionMgr(_FakeAccount(open_positions=[]))
    broker = _FakeBroker(to_close=[])
    notifier = _FakeNotifier()

    result = await _phase_close_sl_tp(_FakeConfigForClose(), pm, broker, notifier, price_provider=None)

    assert result == []
    assert broker.check_called_with == {}, "broker は呼ばれない"
    assert notifier.closed_events == []


@pytest.mark.asyncio
async def test_phase_close_sl_tp_notifies_each_close(monkeypatch):
    """broker がクローズを返したら notifier.notify_order_closed が件数分呼ばれる。"""
    from src.trading_cycle import _phase_close_sl_tp

    # _get_price は価格取得できない環境 (CI 等) でも動くようモック
    # _phase_close_sl_tp は src.cycles.trading にあり、_get_price をそこへ import
    # しているため、当該モジュールの binding を patch する。
    monkeypatch.setattr("src.cycles.trading._get_price", lambda symbol, pp: 150.5)

    pos = _FakePosition("USDJPY=X")
    closed = _FakeClosedOrder("USDJPY=X", realized=200.0, reason="take_profit")
    pm = _FakePositionMgr(_FakeAccount(open_positions=[pos]))
    broker = _FakeBroker(to_close=[closed])
    notifier = _FakeNotifier()

    result = await _phase_close_sl_tp(_FakeConfigForClose(), pm, broker, notifier, price_provider=None)

    assert len(result) == 1
    assert result[0] is closed
    assert len(notifier.closed_events) == 1
    event = notifier.closed_events[0]
    assert event.pair == "USDJPY=X"
    assert event.realized_pnl == 200.0
    assert event.close_reason == "take_profit"


# ── _finalize_closed_orders ───────────────────────────────────


class _FakePairCfg:
    symbol = "USDJPY=X"
    display_name = "USD/JPY"


class _FakeTradeableCfg:
    """最小限の AppConfig モック。load_user_notes が `.exists()` を呼ぶので
    存在しない Path を渡して空文字列を返させる。"""
    from pathlib import Path as _Path

    tradeable_instruments = [_FakePairCfg()]
    user_notes_path = _Path("/tmp/_pytest_nonexistent_user_notes.md")

    class llm:
        class reflection:
            temperature = 0.3


class _FakeOrderForFinalize:
    order_id = "test-order-1"
    pair = "USDJPY=X"
    direction = "buy"
    entry_price = 150.0
    stop_loss = 149.0
    take_profit = 152.0
    close_price = 151.5
    close_reason = "take_profit"
    realized_pnl = 150.0
    closed_at = None
    signal_reason = "bullish momentum"


class _FakeReflection:
    atr_params_suggestion = None
    full_text = "trade won on momentum"


class _FakeAdaptiveStore:
    def __init__(self):
        self.updates: list = []

    def get_history(self, pair, limit=3):
        return []

    def update_params(self, pair, new_params, reason, trade_id):
        self.updates.append({"pair": pair, "new_params": new_params, "reason": reason})


class _FakeSessionStore:
    def __init__(self):
        self.closed: list = []

    def get_session(self, order_id):
        return None  # entry_analysis / sltp_comparison は空文字に

    def close_session(self, **kwargs):
        self.closed.append(kwargs)


@pytest.mark.asyncio
async def test_finalize_closed_orders_empty_list_is_noop(monkeypatch):
    from src.trading_cycle import _finalize_closed_orders

    called = []

    async def _fake_reflect(**kwargs):
        called.append(kwargs)
        return _FakeReflection()

    monkeypatch.setattr("src.cycles.trading.generate_close_reflection", _fake_reflect)

    await _finalize_closed_orders(
        closed_orders=[],
        config=_FakeTradeableCfg(),
        store=_FakeStoreWithDirectional(_FakeDirectional()),
        embed_fn=_fake_embed,
        llm_reflect=None,
        adaptive_store=_FakeAdaptiveStore(),
        session_store=None,
        log_source="[TEST]",
    )

    assert called == [], "空リストなら generate_close_reflection を呼ばない"


@pytest.mark.asyncio
async def test_finalize_closed_orders_calls_reflection_and_record(monkeypatch):
    """決済済オーダーに対し reflection 生成 → record_trade_complete が呼ばれること。"""
    from src.trading_cycle import _finalize_closed_orders

    reflect_calls = []
    record_calls = []

    async def _fake_reflect(**kwargs):
        reflect_calls.append(kwargs["order"].order_id)
        return _FakeReflection()

    async def _fake_record(store, embed_fn, closed_order, reflection_text):
        record_calls.append({
            "order_id": closed_order.order_id,
            "reflection_text": reflection_text,
        })

    monkeypatch.setattr("src.cycles.trading.generate_close_reflection", _fake_reflect)
    monkeypatch.setattr("src.cycles.trading.record_trade_complete", _fake_record)

    adaptive = _FakeAdaptiveStore()
    session = _FakeSessionStore()

    await _finalize_closed_orders(
        closed_orders=[_FakeOrderForFinalize()],
        config=_FakeTradeableCfg(),
        store=_FakeStoreWithDirectional(_FakeDirectional()),
        embed_fn=_fake_embed,
        llm_reflect=None,
        adaptive_store=adaptive,
        session_store=session,
        log_source="[TEST]",
    )

    assert reflect_calls == ["test-order-1"]
    assert len(record_calls) == 1
    assert record_calls[0]["order_id"] == "test-order-1"
    assert record_calls[0]["reflection_text"] == "trade won on momentum"
    # reflection に atr_params_suggestion がないので update_params 呼ばれない
    assert adaptive.updates == []
    # session_store.close_session は 1 回呼ばれる
    assert len(session.closed) == 1
