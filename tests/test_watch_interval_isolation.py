"""watch 銘柄の収集経路 1h 固定 (spec §9 item 5 fix): day 化の影響を受けない。"""
import pandas as pd

from src.config.schema import AppConfig, InstrumentConfig
from src.jobs.technical_collector import _ohlcv_interval_for


def _trade_inst() -> InstrumentConfig:
    return InstrumentConfig(
        symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
        mode="trade", base_currency="USD", quote_currency="JPY",
    )


def _watch_inst() -> InstrumentConfig:
    return InstrumentConfig(
        symbol="SPY", display_name="S&P 500 ETF", asset_type="index",
        mode="watch", currency="USD",
    )


def test_trade_follows_config():
    cfg = AppConfig()
    cfg.trading.ohlcv_interval = "15m"
    assert _ohlcv_interval_for(_trade_inst(), cfg) == "15m"


def test_watch_pinned_to_1h():
    cfg = AppConfig()
    cfg.trading.ohlcv_interval = "15m"
    assert _ohlcv_interval_for(_watch_inst(), cfg) == "1h"


def test_mtf_skips_finer_than_base_tf():
    """base=1h で short=15m が来ても skip して他 TF は生成される (crash しない)。"""
    from src.config.schema import AnalysisConfig
    from src.data.mtf import compute_mtf_summaries

    n = 96
    idx = pd.date_range("2026-06-27", periods=n, freq="1h")
    close = [150.0 + (i % 10) * 0.01 for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.02 for c in close],
         "Low": [c - 0.02 for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    timeframes = {
        "long":   {"lookback_days": 15, "interval": "4h",  "enabled": True},
        "medium": {"lookback_days": 4,  "interval": "1h",  "enabled": True},
        "short":  {"lookback_days": 1,  "interval": "15m", "enabled": True},  # day 用設定
    }
    summaries = compute_mtf_summaries(df, AnalysisConfig(), timeframes, base_interval="1h")
    assert "short" not in summaries   # skip された
    assert "long" in summaries
    assert "medium" in summaries
