"""経済指標イベントのSQLiteストア。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Column, DateTime, Float, Integer, String, create_engine, select, update
)
from sqlalchemy.orm import DeclarativeBase, Session

from src.analysis.economic_calendar import EconEvent
from src.utils.clock import db_utc_now as _utc_now_naive

logger = logging.getLogger(__name__)


class _Base(DeclarativeBase):
    pass


class _EconEventRow(_Base):
    __tablename__ = "econ_events"

    event_id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    country = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    importance = Column(Integer, nullable=False)
    event_time = Column(DateTime, nullable=False, index=True)
    actual = Column(Float)
    forecast = Column(Float)
    previous = Column(Float)
    unit = Column(String)
    analyzed = Column(Integer, default=0, nullable=False)
    fetched_at = Column(DateTime, default=_utc_now_naive, nullable=False)


def _to_naive_utc(dt: datetime) -> datetime:
    """datetime を naive UTC に変換する (SQLite DateTime列はnaive前提)。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _row_to_event(row: _EconEventRow) -> EconEvent:
    return EconEvent(
        event_id=row.event_id,
        title=row.title,
        country=row.country,
        currency=row.currency,
        importance=row.importance,
        event_time=row.event_time.replace(tzinfo=timezone.utc),
        actual=row.actual,
        forecast=row.forecast,
        previous=row.previous,
        unit=row.unit or "",
    )


class EconEventStore:
    """経済指標イベントの永続化ストア。"""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)
        _Base.metadata.create_all(self._engine)

    def upsert_events(self, events: list[EconEvent]) -> int:
        """イベントを upsert する。analyzed フラグは既存値を保持。"""
        with Session(self._engine) as session:
            for ev in events:
                existing = session.get(_EconEventRow, ev.event_id)
                if existing:
                    existing.title = ev.title
                    existing.country = ev.country
                    existing.currency = ev.currency
                    existing.importance = ev.importance
                    existing.event_time = _to_naive_utc(ev.event_time)
                    existing.actual = ev.actual
                    existing.forecast = ev.forecast
                    existing.previous = ev.previous
                    existing.unit = ev.unit
                    existing.fetched_at = _utc_now_naive()
                else:
                    row = _EconEventRow(
                        event_id=ev.event_id,
                        title=ev.title,
                        country=ev.country,
                        currency=ev.currency,
                        importance=ev.importance,
                        event_time=_to_naive_utc(ev.event_time),
                        actual=ev.actual,
                        forecast=ev.forecast,
                        previous=ev.previous,
                        unit=ev.unit,
                        analyzed=0,
                        fetched_at=_utc_now_naive(),
                    )
                    session.add(row)
            session.commit()
        return len(events)

    def update_actuals(self, updates: dict[str, dict]) -> int:
        """{event_id: {actual: float}} で actual を更新する。"""
        count = 0
        with Session(self._engine) as session:
            for event_id, fields in updates.items():
                row = session.get(_EconEventRow, event_id)
                if row is None:
                    continue
                if "actual" in fields:
                    row.actual = fields["actual"]
                    count += 1
            session.commit()
        return count

    def get_recent_published(self, lookback_min: int) -> list[EconEvent]:
        """直近 lookback_min 分以内に発表時刻があったイベントを返す。"""
        cutoff = _utc_now_naive() - timedelta(minutes=lookback_min)
        with Session(self._engine) as session:
            stmt = (
                select(_EconEventRow)
                .where(_EconEventRow.event_time >= cutoff)
                .where(_EconEventRow.event_time <= _utc_now_naive())
                .order_by(_EconEventRow.event_time.desc())
            )
            rows = session.execute(stmt).scalars().all()
        return [_row_to_event(r) for r in rows]

    def get_events_in_window(
        self,
        start: datetime,
        end: datetime,
        min_importance: int = 0,
    ) -> list[EconEvent]:
        """event_time が [start, end] に入る min_importance 以上のイベントを返す。

        get_recent_published と異なり `<= now` で clamp しない (未来のイベントも返す)。
        cadence boost / material event window 判定で「これから来る高重要度イベント」を
        先回り検出するために使う (§5.3 経路① / Phase1 A-2)。start/end は naive UTC で渡す。
        """
        s = _to_naive_utc(start)
        e = _to_naive_utc(end)
        with Session(self._engine) as session:
            stmt = (
                select(_EconEventRow)
                .where(_EconEventRow.event_time >= s)
                .where(_EconEventRow.event_time <= e)
                .where(_EconEventRow.importance >= min_importance)
                .order_by(_EconEventRow.event_time.asc())
            )
            rows = session.execute(stmt).scalars().all()
        return [_row_to_event(r) for r in rows]

    def get_unanalyzed_with_actual(
        self,
        lookback_min: int,
        min_importance: int,
    ) -> list[EconEvent]:
        """actualが埋まっていて未分析のイベントを返す。"""
        cutoff = _utc_now_naive() - timedelta(minutes=lookback_min)
        with Session(self._engine) as session:
            stmt = (
                select(_EconEventRow)
                .where(_EconEventRow.event_time >= cutoff)
                .where(_EconEventRow.event_time <= _utc_now_naive())
                .where(_EconEventRow.actual.is_not(None))
                .where(_EconEventRow.importance >= min_importance)
                .where(_EconEventRow.analyzed == 0)
                .order_by(_EconEventRow.event_time.asc())
            )
            rows = session.execute(stmt).scalars().all()
        return [_row_to_event(r) for r in rows]

    def mark_analyzed(self, event_id: str) -> None:
        with Session(self._engine) as session:
            stmt = (
                update(_EconEventRow)
                .where(_EconEventRow.event_id == event_id)
                .values(analyzed=1)
            )
            session.execute(stmt)
            session.commit()
