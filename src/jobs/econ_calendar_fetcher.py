"""経済指標カレンダーのフェッチジョブ。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.analysis.economic_calendar import fetch_events
from src.data.econ_event_store import EconEventStore

logger = logging.getLogger(__name__)

# Currency → Country ISO code mapping
_COUNTRY_MAP = {
    "USD": "US", "JPY": "JP", "EUR": "EU", "GBP": "GB",
    "AUD": "AU", "CAD": "CA", "CHF": "CH", "NZD": "NZ",
}


def run_daily_fetch(
    store: EconEventStore,
    lookahead_hours: int,
    currencies: list[str],
    min_importance: int,
) -> int:
    """日次: 次 lookahead_hours 時間分のイベントを取得してupsert。"""
    countries = list({_COUNTRY_MAP.get(c, c) for c in currencies})

    from_utc = datetime.now(timezone.utc)
    to_utc = from_utc + timedelta(hours=lookahead_hours)

    events = fetch_events(from_utc, to_utc, countries)
    filtered = [e for e in events if e.importance >= min_importance]
    count = store.upsert_events(filtered)
    logger.info(
        f"[ECON] Daily fetch: {len(events)} events fetched, "
        f"{count} stored (min_importance={min_importance})"
    )
    return count


def refresh_recent_events(
    store: EconEventStore,
    lookback_min: int,
    currencies: list[str],
) -> int:
    """毎時: 直近 lookback_min 分の event の actual を再取得して更新。"""
    countries = list({_COUNTRY_MAP.get(c, c) for c in currencies})

    now = datetime.now(timezone.utc)
    from_utc = now - timedelta(minutes=lookback_min + 5)
    to_utc = now + timedelta(minutes=5)

    events = fetch_events(from_utc, to_utc, countries)
    updates = {}
    for ev in events:
        if ev.actual is not None:
            updates[ev.event_id] = {"actual": ev.actual}
    if not updates:
        return 0

    count = store.update_actuals(updates)
    if count > 0:
        logger.info(f"[ECON] Refreshed {count} event actuals")
    return count
