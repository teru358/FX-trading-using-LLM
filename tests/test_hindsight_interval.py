"""hindsight の OHLCV 読み出し足種対応 (codex High#2)。

make_hindsight_evaluator が interval を明示しないと常に "1h" バケットを読み、
day モード (ohlcv_interval=15m) でキャッシュされた 15m 専用データが見えず
"no ohlcv in horizon" になる。interval を明示的に渡した場合に正しいバケットを
読めること、渡さない場合は従来通り "1h" のままであることを確認する。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.orchestrator.hindsight_evaluator import make_hindsight_evaluator

TRIG = datetime(2026, 6, 21, 9, 0, 0)


def _ohlcv_df():
    idx = pd.to_datetime([TRIG + timedelta(minutes=15), TRIG + timedelta(minutes=30)])
    return pd.DataFrame(
        {
            "Open": [150.0, 150.5], "High": [150.8, 151.6],
            "Low": [149.8, 150.4], "Close": [150.7, 151.5], "Volume": [0, 0],
        },
        index=idx,
    )


def _evaluate(evaluator):
    return evaluator.evaluate(
        pair="USDJPY=X", direction="long",
        trigger_price=150.0, sl=149.4, tp=151.5,
        triggered_at=TRIG, horizon_seconds=3600,
    )


def test_matching_interval_sees_data(tmp_path):
    """interval="15m" で書いたデータを interval="15m" の evaluator で読めること。"""
    from src.data.price_store import PriceStore

    ps = PriceStore(tmp_path / "prices.db")
    ps.upsert_ohlcv("USDJPY=X", _ohlcv_df(), interval="15m")

    evaluator = make_hindsight_evaluator(ps, interval="15m")
    res = _evaluate(evaluator)
    assert res.has_data is True


def test_default_interval_misses_15m_only_data(tmp_path):
    """interval 未指定 (既定 "1h") では 15m 専用データが見えず has_data=False。"""
    from src.data.price_store import PriceStore

    ps = PriceStore(tmp_path / "prices.db")
    ps.upsert_ohlcv("USDJPY=X", _ohlcv_df(), interval="15m")

    evaluator = make_hindsight_evaluator(ps)
    res = _evaluate(evaluator)
    assert res.has_data is False
    assert res.reasoning_summary == "no ohlcv in horizon"
