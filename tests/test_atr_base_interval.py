"""ATR の基底足対応 (codex High#1): 15m 基底 + atr_timeframe=1h で 1h ATR になること。"""
import pandas as pd
from types import SimpleNamespace

from src.cycles._helpers import _compute_atr_from_price_data


def _pd15(n=200, amp=0.1):
    idx = pd.date_range("2026-07-01", periods=n, freq="15min")
    close = [150.0 + (i % 8) * amp for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + amp for c in close],
         "Low": [c - amp for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    return SimpleNamespace(df=df)


def test_1h_atr_from_15m_base_resamples():
    """base=15m のとき resample_tf='1h' は必ずリサンプルされ、15m ATR より大きくなる。"""
    pd15 = _pd15()
    atr_15m = _compute_atr_from_price_data(pd15, resample_tf="", base_interval="15m")
    atr_1h = _compute_atr_from_price_data(pd15, resample_tf="1h", base_interval="15m")
    assert atr_15m is not None and atr_1h is not None
    assert atr_1h > atr_15m  # 1h バーは 4 本分のレンジを含むため必ず広い


def test_default_base_1h_identity_unchanged():
    """従来挙動: base=1h (既定) で resample_tf='1h' はリサンプルなし (挙動不変)。"""
    idx = pd.date_range("2026-07-01", periods=200, freq="1h")
    close = [150.0 + (i % 8) * 0.1 for i in range(200)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.1 for c in close],
         "Low": [c - 0.1 for c in close], "Close": close, "Volume": [1.0] * 200},
        index=idx,
    )
    pdata = SimpleNamespace(df=df)
    assert _compute_atr_from_price_data(pdata, resample_tf="1h") == \
           _compute_atr_from_price_data(pdata, resample_tf="")


def test_vol_regime_path_resamples_15m_base_to_1h(monkeypatch):
    """vol_regime 経路 (twin bug): base=15m + atr_timeframe='1h' で compute_vol_regime
    に渡る df が 1h にリサンプルされていること (従来は '1h' 除外でスキップされ 15m のまま)。
    """
    from datetime import datetime

    from src.config import load_config
    from src.cycles.trading import _apply_atr_sltp_to_signal
    from src.data.price_fetcher import PriceData

    config = load_config()
    monkeypatch.setattr(config.trading, "ohlcv_interval", "15m")
    monkeypatch.setattr(config.trading, "atr_timeframe", "1h")
    monkeypatch.setattr(config.trading, "vol_regime_enabled", True)
    monkeypatch.setattr(config.trading, "min_rr_ratio", 0.0)  # 早期 return を避ける

    pd15 = _pd15(n=200)
    price_data = PriceData(
        symbol="USDJPY=X", df=pd15.df,
        current_price=float(pd15.df["Close"].iloc[-1]), fetched_at=datetime.now(),
    )
    monkeypatch.setattr(
        "src.cycles._helpers.fetch_ohlcv",
        lambda symbol, period, interval, price_store=None: price_data,
    )

    captured = {}

    def _fake_vol_regime(df, **kwargs):
        captured["df"] = df
        return None  # risk 倍率適用なし (計算入力の検証のみ)

    monkeypatch.setattr("src.signals.vol_regime.compute_vol_regime", _fake_vol_regime)

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

    class _AdaptiveStore:
        def get_params(self, pair):
            return {"sl_atr_mult": 1.5, "tp_atr_mult": 2.5}

    class _DummyStore:
        pass

    result = _apply_atr_sltp_to_signal(
        _Signal(), config, _PM(), _DummyStore(), _AdaptiveStore(),
        price_provider=None,
    )
    assert result is not None, "SLTPResult should be returned"
    vr_df = captured["df"]
    # 15m 基底が 1h にリサンプルされていること (バー間隔 1h、本数 ≈ 1/4)
    deltas = vr_df.index.to_series().diff().dropna()
    assert (deltas == pd.Timedelta(hours=1)).all()
    assert len(vr_df) == len(pd15.df) // 4
