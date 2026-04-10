"""TradingView 経済指標カレンダー API クライアント。

非公式API (economic-calendar.tradingview.com) からイベントを取得し、
サプライズ度を計算する純粋関数群。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://economic-calendar.tradingview.com/events"
_HEADERS = {"Origin": "https://www.tradingview.com"}


@dataclass
class EconEvent:
    """経済指標イベント。"""
    event_id: str
    title: str
    country: str
    currency: str
    importance: int           # -1=low, 0=medium, 1=high
    event_time: datetime      # UTC
    actual: float | None
    forecast: float | None
    previous: float | None
    unit: str


def parse_event(raw: dict[str, Any]) -> EconEvent:
    """TV API レスポンスの1イベントを EconEvent に変換する。"""
    def _f(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    date_str = raw.get("date", "").replace("Z", "+00:00")
    try:
        event_time = datetime.fromisoformat(date_str)
    except ValueError:
        event_time = datetime.now(timezone.utc)

    return EconEvent(
        event_id=str(raw.get("id", "")),
        title=str(raw.get("title", "")),
        country=str(raw.get("country", "")),
        currency=str(raw.get("currency", "")),
        importance=int(raw.get("importance", -1)),
        event_time=event_time,
        actual=_f(raw.get("actual")),
        forecast=_f(raw.get("forecast")),
        previous=_f(raw.get("previous")),
        unit=str(raw.get("unit", "")),
    )


def fetch_events(
    from_utc: datetime,
    to_utc: datetime,
    countries: list[str],
    timeout: float = 10.0,
) -> list[EconEvent]:
    """TV API から指定期間のイベントを取得する。"""
    params = {
        "from": from_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": to_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "countries": ",".join(countries),
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(_API_URL, headers=_HEADERS, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[ECON] fetch_events failed: {e}")
        return []

    if data.get("status") != "ok":
        logger.warning(f"[ECON] API status not ok: {data.get('status')}")
        return []

    events = []
    for raw in data.get("result", []):
        try:
            events.append(parse_event(raw))
        except Exception as e:
            logger.debug(f"[ECON] parse failed for {raw.get('id')}: {e}")
    return events


def classify_surprise(actual: float | None, forecast: float | None) -> str | None:
    """実測値と予測値からサプライズ度を分類する。

    Returns:
        "strong_beat" | "beat" | "miss" | "strong_miss" | None
    """
    if actual is None or forecast is None or forecast == 0:
        return None
    surprise_pct = (actual - forecast) / abs(forecast) * 100
    if surprise_pct >= 10:
        return "strong_beat"
    if surprise_pct > 0:
        return "beat"
    if surprise_pct > -10:
        return "miss"
    return "strong_miss"
