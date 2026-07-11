"""OHLCV価格データのSQLiteキャッシュストア。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Column, DateTime, Float, String, create_engine, inspect as sa_inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session

from src.utils.clock import to_db_naive_datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared engine cache — PriceStore / AnalysisStore が同一DBを共有する
# ---------------------------------------------------------------------------
_engines: dict[str, Any] = {}


class _Base(DeclarativeBase):
    pass


def _migrate_schema(engine) -> None:
    """旧スキーマを新スキーマに自動移行する (cache のため DROP→再作成)。"""
    inspector = sa_inspect(engine)
    if "ohlcv" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("ohlcv")}
        if ("date" in cols and "bar_time" not in cols) or "interval" not in cols:
            with engine.connect() as conn:
                conn.execute(text("DROP TABLE ohlcv"))
                conn.commit()
            logger.warning(
                "OHLCV table migrated: schema changed (cache cleared, will refetch)"
            )


def _get_engine(db_path: Path):
    # db_path が Path/str 以外 (テストの未設定 MagicMock config を store に渡す等) だと、
    # str(mock) がファイル名として扱われ作業ディレクトリに `<MagicMock ...>` SQLite が
    # 量産される。全 store 共通の engine 入口で早期に弾き汚染源を断つ。
    if not isinstance(db_path, (str, Path)):
        raise TypeError(
            f"db_path must be a str or Path, got {type(db_path).__name__}"
        )
    key = str(Path(db_path).resolve())
    if key not in _engines:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        _migrate_schema(engine)
        _engines[key] = engine
        logger.info(f"PriceDB engine ready: {db_path}")
    # create_all は既存テーブルをスキップするため毎回呼んでも安全。
    # 別モジュール（analysis_store等）が後から _Base にモデルを追加した場合にも対応。
    _Base.metadata.create_all(_engines[key])
    return _engines[key]


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------

class _OhlcvRow(_Base):
    __tablename__ = "ohlcv"

    symbol   = Column(String,   primary_key=True)
    interval = Column(String,   primary_key=True, default="1h")  # "1h" | "15m" 等 (spec S-4a)
    bar_time = Column(DateTime, primary_key=True)  # datetime（各足種共用）
    open     = Column(Float)
    high     = Column(Float)
    low      = Column(Float)
    close    = Column(Float)
    volume   = Column(Float)


# ---------------------------------------------------------------------------
# PriceStore — OHLCV cache
# ---------------------------------------------------------------------------

class PriceStore:
    """SQLite backed OHLCV price cache (SQLAlchemy 2.0)."""

    def __init__(self, db_path: Path) -> None:
        self._engine = _get_engine(db_path)

    def upsert_ohlcv(self, symbol: str, df: pd.DataFrame, *, interval: str = "1h") -> None:
        """DataFrameのOHLCVをDBに差分保存（同一 symbol+interval+bar_time は上書き）。"""
        if df.empty:
            return
        with Session(self._engine) as session:
            for ts, row in df.iterrows():
                bar_time: datetime = (
                    ts.to_pydatetime() if hasattr(ts, "to_pydatetime")
                    else datetime(ts.year, ts.month, ts.day)
                )
                # DB 規約 (naive machine-local) に正規化。aware UTC を直に剥がすと
                # 9h ズレるため共通ヘルパで UTC→ローカル変換してから naive 化する。
                bar_time = to_db_naive_datetime(bar_time)
                obj = _OhlcvRow(
                    symbol=symbol,
                    interval=interval,
                    bar_time=bar_time,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                )
                session.merge(obj)
            session.commit()
        logger.debug(f"Upserted {len(df)} OHLCV rows for {symbol}")

    def load_ohlcv(self, symbol: str, start: datetime, end: datetime, *, interval: str = "1h") -> pd.DataFrame:
        """指定期間・指定 interval (足種, 既定 1h) のOHLCVをDataFrame (DatetimeIndex) で返す。"""
        with Session(self._engine) as session:
            stmt = (
                select(_OhlcvRow)
                .where(_OhlcvRow.symbol == symbol)
                .where(_OhlcvRow.interval == interval)
                .where(_OhlcvRow.bar_time >= start)
                .where(_OhlcvRow.bar_time <= end)
                .order_by(_OhlcvRow.bar_time)
            )
            rows = session.execute(stmt).scalars().all()

        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        return pd.DataFrame(
            {
                "Open":   [r.open   for r in rows],
                "High":   [r.high   for r in rows],
                "Low":    [r.low    for r in rows],
                "Close":  [r.close  for r in rows],
                "Volume": [r.volume for r in rows],
            },
            index=pd.to_datetime([r.bar_time for r in rows]),
        )

    def get_latest_date(self, symbol: str, *, interval: str = "1h") -> datetime | None:
        """指定 interval (足種, 既定 1h) の最新 bar_time を返す。データなしなら None。"""
        with Session(self._engine) as session:
            stmt = (
                select(_OhlcvRow.bar_time)
                .where(_OhlcvRow.symbol == symbol)
                .where(_OhlcvRow.interval == interval)
                .order_by(_OhlcvRow.bar_time.desc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()

    def get_earliest_date(self, symbol: str, *, interval: str = "1h") -> datetime | None:
        """指定 interval (足種, 既定 1h) の最古 bar_time を返す。データなしなら None。"""
        with Session(self._engine) as session:
            stmt = (
                select(_OhlcvRow.bar_time)
                .where(_OhlcvRow.symbol == symbol)
                .where(_OhlcvRow.interval == interval)
                .order_by(_OhlcvRow.bar_time.asc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()
