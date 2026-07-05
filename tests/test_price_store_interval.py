"""PriceStore interval 列 (spec S-4a): 1h/15m がキー共存し混在しないこと。"""
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from src.data.price_store import PriceStore


@pytest.fixture
def store(tmp_path):
    return PriceStore(tmp_path / "price.db")


def _df(times, base=100.0):
    return pd.DataFrame(
        {
            "Open": [base] * len(times),
            "High": [base + 1] * len(times),
            "Low": [base - 1] * len(times),
            "Close": [base + 0.5] * len(times),
            "Volume": [0.0] * len(times),
        },
        index=pd.to_datetime(times),
    )


def test_intervals_do_not_mix(store):
    t = datetime(2026, 7, 1, 10, 0)
    store.upsert_ohlcv("USDJPY=X", _df([t]), interval="1h")
    store.upsert_ohlcv("USDJPY=X", _df([t], base=200.0), interval="15m")

    df_1h = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2), interval="1h")
    df_15m = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2), interval="15m")
    assert len(df_1h) == 1 and len(df_15m) == 1
    assert df_1h["Close"].iloc[0] == pytest.approx(100.5)
    assert df_15m["Close"].iloc[0] == pytest.approx(200.5)


def test_latest_earliest_are_per_interval(store):
    store.upsert_ohlcv("USDJPY=X", _df([datetime(2026, 7, 1, 10)]), interval="1h")
    store.upsert_ohlcv("USDJPY=X", _df([datetime(2026, 7, 1, 12)]), interval="15m")
    assert store.get_latest_date("USDJPY=X", interval="1h") == datetime(2026, 7, 1, 10)
    assert store.get_latest_date("USDJPY=X", interval="15m") == datetime(2026, 7, 1, 12)
    assert store.get_latest_date("USDJPY=X", interval="4h") is None


def test_migration_drops_pre_interval_table(tmp_path):
    """interval 列なし旧テーブルは DROP→再作成される (cache クリア, spec S-4a)。

    本番 DB (stick/Fiosracht) が次回デプロイで通る経路の回帰テスト。
    """
    db_path = tmp_path / "legacy_price.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE ohlcv ("
            "symbol TEXT NOT NULL, bar_time TIMESTAMP NOT NULL, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL, "
            "PRIMARY KEY (symbol, bar_time))"
        )
        conn.execute(
            "INSERT INTO ohlcv VALUES ('USDJPY=X', '2026-06-30 09:00:00', 99, 100, 98, 99.5, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    # (a) 例外なく開ける (migration が DROP→再作成)
    store = PriceStore(db_path)

    # (c) 旧 dummy row は消えている (cache クリア)
    assert store.get_latest_date("USDJPY=X", interval="1h") is None
    df_old = store.load_ohlcv("USDJPY=X", datetime(2026, 6, 1), datetime(2026, 7, 2))
    assert df_old.empty

    # (b) migration 後は interval 対応で通常動作する
    t = datetime(2026, 7, 1, 10, 0)
    store.upsert_ohlcv("USDJPY=X", _df([t], base=200.0), interval="15m")
    df = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2), interval="15m")
    assert len(df) == 1
    assert df["Close"].iloc[0] == pytest.approx(200.5)


def test_default_interval_is_1h_backward_compat(store):
    """interval 未指定の既存呼び出しは 1h として動く (挙動不変)。"""
    t = datetime(2026, 7, 1, 10, 0)
    store.upsert_ohlcv("USDJPY=X", _df([t]))
    df = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2))
    assert len(df) == 1
    assert store.get_latest_date("USDJPY=X") == t
    assert store.get_earliest_date("USDJPY=X") == t
