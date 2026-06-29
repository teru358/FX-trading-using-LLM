"""PriceStore.upsert_ohlcv の DB 境界 tz 正規化テスト。

別 provider (TwelveData 等) が _normalize_index を通さず aware index を直接渡しても、
永続化境界で naive ローカルに揃うことを保証する。
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from src.data.price_store import PriceStore
import src.utils.clock as clock

_JST = ZoneInfo("Asia/Tokyo")


def test_upsert_ohlcv_aware_utc_stored_as_local_naive(tmp_path, monkeypatch):
    """aware UTC index を渡しても DB には naive ローカル時刻で保存される。"""
    monkeypatch.setattr(clock, "_resolve_local_tz", lambda tz=None: _JST)
    store = PriceStore(tmp_path / "p.db")

    idx = pd.DatetimeIndex(["2026-06-29 10:00:00"], tz="UTC")
    df = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                       "Close": [1.0], "Volume": [1]}, index=idx)
    store.upsert_ohlcv("USDJPY=X", df)

    got = store.load_ohlcv("USDJPY=X",
                           pd.Timestamp("2026-06-29 18:00:00"),
                           pd.Timestamp("2026-06-29 20:00:00"))
    assert len(got) == 1
    assert got.index[-1] == pd.Timestamp("2026-06-29 19:00:00")
