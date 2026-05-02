"""bridge /ohlcv 経由の OHLCV 取得。既存 fetch_ohlcv のパターン踏襲。

PriceStore (単一 SQLite) を共有して DB 差分取得をベースとする:
- 初回 (DB 空) は required_start から full backfill
- 2 回目以降は最新バー以降の差分のみ取得
- bridge 到達不可は Mt5UnreachableError → 上位で chain フォールバック判断
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from src.data.price_fetcher import PriceData
from src.trading.symbol_mapping import to_mt5_symbol

if TYPE_CHECKING:
    from src.data.price_store import PriceStore

logger = logging.getLogger(__name__)


class Mt5UnreachableError(Exception):
    """bridge への到達不可を示す例外 (上位で chain フォールバック判断)。"""


def _to_utc(dt: datetime) -> datetime:
    """tz-naive (local) datetime を tz-aware UTC に変換する。

    既に tz-aware な場合は astimezone(UTC) で正規化。tz-naive は astimezone()
    がデフォルトでローカル TZ 起点とみなして UTC に変換するため、JST/UTC を
    跨いで一貫した動作になる。
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)    # naive → astimezone() がローカル TZ から UTC に


def _parse_period_days(period: str) -> int:
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo"):
        return int(period[:-2]) * 30
    if period.endswith("y"):
        return int(period[:-1]) * 365
    return 90


class Mt5OhlcvFetcher:
    def __init__(
        self, *, bridge_url: str, request_timeout: float,
        api_key: str = "",
    ) -> None:
        self._url = bridge_url.rstrip("/")
        self._timeout = request_timeout
        self._headers = (
            {"X-Bridge-Api-Key": api_key} if api_key else {}
        )

    def fetch(
        self, symbol: str, *,
        period: str = "90d", interval: str = "1h",
        price_store: "PriceStore | None" = None,
    ) -> PriceData:
        days = _parse_period_days(period)
        required_start = datetime.now() - timedelta(days=days + 1)

        if price_store is not None:
            # Step 1: 過去方向の補完 (DB が空なら required_start から、あれば最古から遡る)
            latest = price_store.get_latest_date(symbol)
            hist_start = (
                latest - timedelta(days=days + 1) if latest is not None
                else required_start
            )
            earliest = price_store.get_earliest_date(symbol)
            if earliest is None or earliest > hist_start + timedelta(hours=2):
                self._fetch_and_upsert(
                    symbol, hist_start, datetime.now(),
                    interval=interval, price_store=price_store,
                )

            # Step 2: 差分フェッチ (最新バー以降)
            latest = price_store.get_latest_date(symbol)
            if latest is not None:
                fetch_from = latest + timedelta(hours=1)
                if fetch_from <= datetime.now():
                    self._fetch_and_upsert(
                        symbol, fetch_from, datetime.now(),
                        interval=interval, price_store=price_store,
                    )

            df = price_store.load_ohlcv(symbol, required_start, datetime.now())
            if len(df) >= 20:
                current_price = float(df["Close"].iloc[-1])
                logger.info(
                    f"[MT5] Loaded {len(df)} bars for {symbol} from DB, "
                    f"latest close={current_price:.5f}"
                )
                return PriceData(
                    symbol=symbol, df=df, current_price=current_price,
                    fetched_at=datetime.now(),
                )
            logger.warning(f"[MT5] DB has only {len(df)} bars for {symbol}, full fetch")

        # フォールバック: フルフェッチ
        df = self._fetch_dataframe(
            symbol, datetime.now() - timedelta(days=days), datetime.now(),
            interval=interval,
        )
        if len(df) < 20:
            raise Mt5UnreachableError(
                f"insufficient data from MT5 for {symbol}: {len(df)} bars"
            )

        if price_store is not None:
            price_store.upsert_ohlcv(symbol, df)

        current_price = float(df["Close"].iloc[-1])
        return PriceData(
            symbol=symbol, df=df, current_price=current_price,
            fetched_at=datetime.now(),
        )

    def _fetch_and_upsert(
        self, symbol: str, start: datetime, end: datetime, *,
        interval: str, price_store: "PriceStore",
    ) -> None:
        df = self._fetch_dataframe(symbol, start, end, interval=interval)
        if not df.empty:
            price_store.upsert_ohlcv(symbol, df)
            logger.info(
                f"[MT5] Stored {len(df)} bars for {symbol} ({start:%Y-%m-%d}〜)"
            )

    def _fetch_dataframe(
        self, symbol: str, start: datetime, end: datetime, *,
        interval: str,
    ) -> pd.DataFrame:
        mt5_symbol = to_mt5_symbol(symbol)
        # naive datetime (= local time, JST 環境では JST) を UTC に**変換**して送る。
        # 旧実装は replace(tzinfo=utc) でラベル付け替えだけしており、JST で 9 時間
        # 先の未来時刻を bridge に要求して 0 バーが返る不具合があった。
        params = {
            "from": _to_utc(start).isoformat(),
            "to":   _to_utc(end).isoformat(),
            "interval": interval,
        }
        try:
            resp = httpx.get(
                f"{self._url}/ohlcv/{mt5_symbol}",
                params=params, timeout=self._timeout,
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise Mt5UnreachableError(f"bridge /ohlcv failed: {e}") from e

        bars = resp.json().get("bars", [])
        if not bars:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        df = pd.DataFrame([{
            "_time": b["time"],
            "Open":  b["open"], "High": b["high"],
            "Low":   b["low"],  "Close": b["close"],
            "Volume": b["volume"],
        } for b in bars])
        df["_time"] = pd.to_datetime(df["_time"]).dt.tz_convert(None)
        df = df.set_index("_time")
        df.index.name = None
        return df
