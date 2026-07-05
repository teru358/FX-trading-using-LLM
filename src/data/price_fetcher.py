from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from src.data.resample import interval_minutes
from src.utils.clock import to_db_naive_index

if TYPE_CHECKING:
    from src.data.price_store import PriceStore

logger = logging.getLogger(__name__)

# yfinance内部ロガーのレベルを上げ、閉場時の "possibly delisted" ERROR を抑止
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class PriceData:
    symbol: str
    df: pd.DataFrame  # OHLCV with DatetimeIndex
    current_price: float
    fetched_at: datetime


@dataclass
class CurrentPrice:
    """現在価格 + オプション付加情報（Twelve Data 等リアルタイムプロバイダー用）。"""
    price: float
    timestamp: datetime
    percent_change: float | None = None
    rolling_1d_change: float | None = None
    rolling_7d_change: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    is_market_open: bool | None = None
    source: str = "yfinance"

    def __float__(self) -> float:
        return self.price


def _parse_period_days(period: str) -> int:
    """'90d' → 90、'3mo' → 90 のように日数に変換する。"""
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo"):
        return int(period[:-2]) * 30
    if period.endswith("y"):
        return int(period[:-1]) * 365
    return 90


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance の tz-aware index を DB 規約 (naive machine-local) に正規化する。

    tz_convert(None) は naive UTC を作り db_now() と 9h ズレるため使わない
    (to_db_naive_index が UTC→ローカル変換してから剥がす)。
    """
    df.index = to_db_naive_index(pd.to_datetime(df.index))
    return df


def _is_intraday(interval: str) -> bool:
    """1d より短い足かどうか判定する。"""
    return not interval.endswith("d") and not interval.endswith("wk") and not interval.endswith("mo")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _yf_fetch_period(symbol: str, period: str, interval: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    try:
        df = ticker.history(period=period, interval=interval)
    except Exception as e:
        err_msg = str(e)
        if "no price data found" in err_msg or "Data doesn't exist" in err_msg:
            logger.debug(f"{symbol}: no data available (period={period}), market likely closed")
            raise ValueError(f"No data returned for {symbol} (market closed)") from e
        raise
    if df.empty:
        raise ValueError(f"No data returned for {symbol}")
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    return _normalize_index(df)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _yf_fetch_range(symbol: str, start: datetime, interval: str) -> pd.DataFrame:
    """start 以降のデータのみ yfinance から取得する（差分フェッチ用）。

    yfinance は市場閉場中や非取引日に空DataFrameを返すことが多い（特にindex系）。
    その場合はリトライせず空を返す。
    """
    ticker = yf.Ticker(symbol)
    end = datetime.now() + timedelta(hours=2)  # 最新バーも取得するため余裕を持たせる
    try:
        df = ticker.history(start=start, end=end, interval=interval)
    except Exception as e:
        err_msg = str(e)
        # yfinance が "no price data found" / "Data doesn't exist" を返す場合は
        # 市場閉場・非取引日の可能性が高いためリトライしない
        if "no price data found" in err_msg or "Data doesn't exist" in err_msg:
            logger.debug(f"{symbol}: no data available ({start:%m-%d %H:%M} -> now), market likely closed")
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        raise
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    return _normalize_index(df)


def fetch_ohlcv(
    symbol: str,
    period: str = "90d",
    interval: str = "1h",
    price_store: "PriceStore | None" = None,
) -> PriceData:
    """
    OHLCVを取得する。price_store が渡された場合はキャッシュ優先で差分のみ取得する。

    フロー（price_store あり）:
      1. DBの最古bar_timeを確認し、required_start まで遡るバックフィルを実行（過去方向の補完）
      2. DBの最新bar_timeを確認し、その次以降を yfinance で差分フェッチ → upsert（未来方向の補完）
      3. DBから必要期間をロード
      4. データ不足なら yfinance でフルフェッチ → upsert してリターン
    """
    logger.debug(f"Fetching OHLCV for {symbol}, period={period}, interval={interval}")

    if price_store is not None:
        days = _parse_period_days(period)
        required_start = datetime.now() - timedelta(days=days + 1)

        # Step 1: 最新データから遡って過去方向の補完
        # DBの最新バーを基点に必要期間を遡り、最古バーが不足していればバックフィルする
        latest = price_store.get_latest_date(symbol, interval=interval)
        hist_start = (
            latest - timedelta(days=days + 1)
            if latest is not None
            else required_start  # DB空の場合は現在から遡る
        )
        earliest = price_store.get_earliest_date(symbol, interval=interval)
        if earliest is None or earliest > hist_start + timedelta(hours=2):
            try:
                hist_df = _yf_fetch_range(symbol, hist_start, interval)
                if not hist_df.empty:
                    price_store.upsert_ohlcv(symbol, hist_df, interval=interval)
                    logger.info(
                        f"Backfill: stored {len(hist_df)} bars for {symbol} "
                        f"since {hist_start:%Y-%m-%d}"
                    )
            except Exception as e:
                logger.warning(f"Historical backfill failed for {symbol}: {e}")

        # Step 2: 差分フェッチ（最新バー以降の未取得分）
        # 刻みは interval 連動 (codex Med#1: 1h 固定だと 15m で 45 分取り逃がす)。
        # 不明な interval は従来通り 1h にフォールバックする。
        latest = price_store.get_latest_date(symbol, interval=interval)
        if latest is not None:
            step = (
                timedelta(minutes=interval_minutes(interval) or 60)
                if _is_intraday(interval) else timedelta(days=1)
            )
            fetch_from = latest + step
            if fetch_from <= datetime.now():
                try:
                    new_df = _yf_fetch_range(symbol, fetch_from, interval)
                    if not new_df.empty:
                        price_store.upsert_ohlcv(symbol, new_df, interval=interval)
                        logger.info(f"Stored {len(new_df)} new bars for {symbol}")
                except Exception as e:
                    logger.warning(f"Incremental fetch failed for {symbol}: {e}")

        # DBからロード
        df = price_store.load_ohlcv(symbol, required_start, datetime.now(), interval=interval)
        if len(df) >= 20:
            current_price = float(df["Close"].iloc[-1])
            logger.info(f"Loaded {len(df)} bars for {symbol} from DB, latest close={current_price:.5f}")
            return PriceData(
                symbol=symbol,
                df=df,
                current_price=current_price,
                fetched_at=datetime.now(),
            )

        logger.warning(f"DB has only {len(df)} bars for {symbol}, falling back to full yfinance fetch")

    # フォールバック: yfinance からフルフェッチ
    df = _yf_fetch_period(symbol, period, interval)
    if len(df) < 20:
        raise ValueError(f"Insufficient data for {symbol}: only {len(df)} bars")

    if price_store is not None:
        price_store.upsert_ohlcv(symbol, df, interval=interval)

    current_price = float(df["Close"].iloc[-1])
    logger.info(f"Fetched {len(df)} bars for {symbol} from yfinance, latest close={current_price:.5f}")
    return PriceData(
        symbol=symbol,
        df=df,
        current_price=current_price,
        fetched_at=datetime.now(),
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    reraise=True,
)
def fetch_current_price(symbol: str) -> CurrentPrice:
    ticker = yf.Ticker(symbol)
    try:
        price = ticker.fast_info["last_price"]
        if price and price > 0:
            return CurrentPrice(price=float(price), timestamp=datetime.now(), source="yfinance")
    except Exception:
        pass
    # Fallback: use latest close from short history
    df = ticker.history(period="2d", interval="1d")
    if df.empty:
        raise ValueError(f"Cannot fetch current price for {symbol}")
    return CurrentPrice(price=float(df["Close"].iloc[-1]), timestamp=datetime.now(), source="yfinance")
