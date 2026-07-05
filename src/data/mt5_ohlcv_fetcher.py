"""bridge /ohlcv 経由の OHLCV 取得。既存 fetch_ohlcv のパターン踏襲。

PriceStore (単一 SQLite) を共有して DB 差分取得をベースとする:
- 初回 (DB 空) は required_start から full backfill
- 2 回目以降は最新バー以降の差分のみ取得
- bridge 到達不可は Mt5UnreachableError → 上位で chain フォールバック判断
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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


@dataclass
class Quote:
    """bridge /quote 由来のリアルタイム bid/ask quote。

    spread は価格差 (ask-bid)。spread_pips は診断/ログ用 (watch には渡さない、spec §4.5)。
    observed_at は DB 規約 naive local。
    """
    bid: float
    ask: float
    mid: float
    spread: float          # ask - bid (価格差)
    spread_pips: float     # 診断用
    observed_at: datetime
    source: str = "mt5"


def _to_utc(dt: datetime) -> datetime:
    """tz-naive (local) datetime を tz-aware UTC に変換する。

    既に tz-aware な場合は astimezone(UTC) で正規化。tz-naive は astimezone()
    がデフォルトでローカル TZ 起点とみなして UTC に変換するため、JST/UTC を
    跨いで一貫した動作になる。
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)    # naive → astimezone() がローカル TZ から UTC に


def _bridge_times_to_local_naive(times, local_tz) -> "pd.DatetimeIndex":
    """ブリッジ /ohlcv が返す UTC バー時刻を DB 規約の naive ローカル時刻へ変換する。

    DB 規約 (src/utils/clock.py 参照) は naive machine-local。ブリッジは UTC で
    バー時刻を返すため、``utc=True`` で確定的に UTC として解釈し、ローカル TZ へ
    変換してから tz を剥がす。``tz_convert(None)`` (= naive UTC) を直に使うと
    JST 環境で db_now() との比較が 9 時間ズレ、鮮度チェックが新鮮なバーまで
    stale_price 扱いする不具合になる。

    Args:
        times: ブリッジ応答のバー時刻 (ISO 文字列等の iterable)。
        local_tz: 変換先のローカルタイムゾーン (例: ``ZoneInfo("Asia/Tokyo")``)。
    """
    return (
        pd.to_datetime(list(times), utc=True)
        .tz_convert(local_tz)
        .tz_localize(None)
    )


def _bridge_time_to_local_naive(iso_str: str) -> datetime:
    """bridge の UTC aware ISO 時刻 1 件を DB 規約 (naive machine-local) へ変換。

    _bridge_times_to_local_naive の単一値版。aware のまま QuoteSnapshot に入れると
    runtime が naive now との引き算で TypeError → quote_age_sec=None → freshness wall
    が全 block する (spec §3 H1)。
    """
    aware = datetime.fromisoformat(iso_str)
    if aware.tzinfo is None:
        # 既に naive ならそのまま (bridge 仕様変更への保険)
        return aware
    local_tz = datetime.now().astimezone().tzinfo
    return aware.astimezone(local_tz).replace(tzinfo=None)


def _parse_period_days(period: str) -> int:
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo"):
        return int(period[:-2]) * 30
    if period.endswith("y"):
        return int(period[:-1]) * 365
    return 90


def _interval_delta(interval: str) -> timedelta:
    """interval 文字列 ("15m"/"1h"/"1d") を差分フェッチの刻みに変換する。

    不明な形式は安全側 (従来の 1h) に倒す。
    """
    try:
        if interval.endswith("m"):
            return timedelta(minutes=int(interval[:-1]))
        if interval.endswith("h"):
            return timedelta(hours=int(interval[:-1]))
        if interval.endswith("d"):
            return timedelta(days=int(interval[:-1]))
    except ValueError:
        pass
    return timedelta(hours=1)


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

    def fetch_current_price(self, symbol: str) -> "CurrentPrice":
        """直近の 1m バーから現在価格を取得 (bridge `/ohlcv` 経由)。

        live_broker=mt5 のとき price_monitor (Layer 4 トレーリング) で
        参照する「現在値」の権威ソース。bridge 不通や 0 バー応答は
        Mt5UnreachableError で raise → 上位で TD/yfinance fallback。
        """
        from src.data.price_fetcher import CurrentPrice

        end = datetime.now()
        start = end - timedelta(minutes=5)
        df = self._fetch_dataframe(symbol, start, end, interval="1m")
        if df.empty:
            raise Mt5UnreachableError(
                f"bridge /ohlcv returned 0 bars for {symbol}"
            )
        return CurrentPrice(
            price=float(df["Close"].iloc[-1]),
            timestamp=datetime.now(),
            source="mt5",
        )

    def get_quote(self, symbol: str) -> "Quote":
        """bridge /quote/{symbol} からリアルタイム bid/ask を取得する (spec §3)。

        symbol は to_mt5_symbol で MT5 形式に変換 (bridge は文字列をそのまま
        symbol_select に使うため変換漏れは 404)。MT5 未接続 (503/404) は
        Mt5UnreachableError に倒す。
        """
        from src.orchestrator.watch_evaluator import _pip_size_for

        mt5_symbol = to_mt5_symbol(symbol)
        try:
            resp = httpx.get(
                f"{self._url}/quote/{mt5_symbol}",
                timeout=self._timeout, headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise Mt5UnreachableError(
                f"bridge /quote failed for {symbol}: {exc}"
            ) from exc
        data = resp.json()
        bid = float(data["bid"])
        ask = float(data["ask"])
        spread = ask - bid
        pip = _pip_size_for(symbol)
        return Quote(
            bid=bid, ask=ask, mid=(bid + ask) / 2.0,
            spread=spread, spread_pips=spread / pip,
            observed_at=_bridge_time_to_local_naive(data["time"]),
            source="mt5",
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
            latest = price_store.get_latest_date(symbol, interval=interval)
            hist_start = (
                latest - timedelta(days=days + 1) if latest is not None
                else required_start
            )
            earliest = price_store.get_earliest_date(symbol, interval=interval)
            if earliest is None or earliest > hist_start + timedelta(hours=2):
                self._fetch_and_upsert(
                    symbol, hist_start, datetime.now(),
                    interval=interval, price_store=price_store,
                )

            # Step 2: 差分フェッチ (最新バー以降、刻みは interval 連動 = spec S-4a)
            latest = price_store.get_latest_date(symbol, interval=interval)
            if latest is not None:
                fetch_from = latest + _interval_delta(interval)
                if fetch_from <= datetime.now():
                    self._fetch_and_upsert(
                        symbol, fetch_from, datetime.now(),
                        interval=interval, price_store=price_store,
                    )

            df = price_store.load_ohlcv(
                symbol, required_start, datetime.now(), interval=interval,
            )
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
            price_store.upsert_ohlcv(symbol, df, interval=interval)

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
            price_store.upsert_ohlcv(symbol, df, interval=interval)
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
        # ブリッジは UTC でバー時刻を返す。DB 規約 (naive machine-local) に
        # 合わせるためローカル TZ へ変換してから tz を剥がす (naive UTC のまま
        # 保存すると db_now() との比較が JST 環境で 9 時間ズレる)。
        local_tz = datetime.now().astimezone().tzinfo
        df["_time"] = _bridge_times_to_local_naive(df["_time"], local_tz)
        df = df.set_index("_time")
        df.index.name = None
        return df
