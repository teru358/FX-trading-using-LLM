from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from src.data.price_store import PriceStore

logger = logging.getLogger(__name__)


@dataclass
class PriceData:
    symbol: str
    df: pd.DataFrame  # OHLCV with DatetimeIndex
    current_price: float
    fetched_at: datetime


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
    """yfinance が返す timezone-aware な index を naive に正規化する。"""
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    df.index = pd.to_datetime(df.index)
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
    df = ticker.history(period=period, interval=interval)
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
    """start 以降のデータのみ yfinance から取得する（差分フェッチ用）。"""
    ticker = yf.Ticker(symbol)
    end = datetime.now() + timedelta(hours=2)  # 最新バーも取得するため余裕を持たせる
    df = ticker.history(start=start.isoformat(), end=end.isoformat(), interval=interval)
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
      1. DBの最新bar_timeを確認し、その次以降を yfinance で差分フェッチ → upsert
      2. DBから必要期間をロード
      3. データ不足なら yfinance でフルフェッチ → upsert してリターン
    """
    logger.debug(f"Fetching OHLCV for {symbol}, period={period}, interval={interval}")

    if price_store is not None:
        days = _parse_period_days(period)
        required_start = datetime.now() - timedelta(days=days + 1)

        latest = price_store.get_latest_date(symbol)  # datetime | None
        if latest is None:
            fetch_from = required_start
        elif _is_intraday(interval):
            fetch_from = latest + timedelta(hours=1)
        else:
            fetch_from = latest + timedelta(days=1)

        # 差分フェッチ（未取得分が存在する場合のみ）
        if fetch_from <= datetime.now():
            try:
                new_df = _yf_fetch_range(symbol, fetch_from, interval)
                if not new_df.empty:
                    price_store.upsert_ohlcv(symbol, new_df)
                    logger.info(f"Stored {len(new_df)} new bars for {symbol}")
            except Exception as e:
                logger.warning(f"Incremental fetch failed for {symbol}: {e}")

        # DBからロード
        df = price_store.load_ohlcv(symbol, required_start, datetime.now())
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
        price_store.upsert_ohlcv(symbol, df)

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
def fetch_current_price(symbol: str) -> float:
    ticker = yf.Ticker(symbol)
    try:
        price = ticker.fast_info["last_price"]
        if price and price > 0:
            return float(price)
    except Exception:
        pass
    # Fallback: use latest close from short history
    df = ticker.history(period="2d", interval="1d")
    if df.empty:
        raise ValueError(f"Cannot fetch current price for {symbol}")
    return float(df["Close"].iloc[-1])
