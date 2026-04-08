"""Twelve Data REST APIフェッチャー。

無料枠: 800 req/日、8 req/分。FXはリアルタイム（遅延なし）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from src.data.price_fetcher import CurrentPrice, PriceData

if TYPE_CHECKING:
    from src.data.price_store import PriceStore

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.twelvedata.com"

# FXシンボル変換: "USDJPY=X" <-> "USD/JPY"
_FX_PATTERN = re.compile(r"^([A-Z]{3})([A-Z]{3})=X$")

# yfinance index/ETF → Twelve Data シンボル変換（Free枠で利用可能なもの）
# 注: SPX はGrow以上プランが必要なため除外
_INDEX_SYMBOL_MAP: dict[str, str] = {
    # GLD はそのまま使えるのでマッピング不要
}


def _symbol_to_twelvedata(symbol: str) -> str:
    """yfinance形式 → Twelve Data形式。"""
    # index/ETFマッピング
    if symbol in _INDEX_SYMBOL_MAP:
        return _INDEX_SYMBOL_MAP[symbol]
    # FXペア変換
    m = _FX_PATTERN.match(symbol)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return symbol


def _symbol_from_twelvedata(td_symbol: str) -> str:
    """Twelve Data形式 → yfinance形式。"""
    if "/" in td_symbol and len(td_symbol) == 7:
        base, quote = td_symbol.split("/")
        return f"{base}{quote}=X"
    return td_symbol


def _interval_to_twelvedata(interval: str) -> str:
    """yfinance interval形式 → Twelve Data形式。"""
    mapping = {"1h": "1h", "4h": "4h", "1d": "1day", "1wk": "1week", "1mo": "1month"}
    return mapping.get(interval, interval)


def _period_to_outputsize(period: str, interval: str) -> int:
    """期間と足種から必要なバー数を概算する。"""
    days = 90
    if period.endswith("d"):
        days = int(period[:-1])
    elif period.endswith("mo"):
        days = int(period[:-2]) * 30
    if interval in ("1h", "1H"):
        return min(5000, days * 24)
    if interval in ("1d", "1D", "1day"):
        return min(5000, days)
    return min(5000, days * 24)


class TwelveDataFetcher:
    """Twelve Data REST APIクライアント。"""

    def __init__(self, api_key: str, timeout: int = 15) -> None:
        if not api_key:
            raise ValueError("TWELVEDATA_API_KEY が設定されていません")
        self._api_key = api_key
        self._timeout = timeout

    def _get_json(self, endpoint: str, params: dict) -> dict:
        """APIリクエストを発行して JSON を返す。"""
        params["apikey"] = self._api_key
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(f"{_BASE_URL}{endpoint}", params=params)
            resp.raise_for_status()
            data = resp.json()
        if "code" in data and data.get("status") == "error":
            raise ValueError(f"Twelve Data error: {data.get('message', data)}")
        return data

    def fetch_current_price(self, symbol: str) -> CurrentPrice:
        """quote エンドポイントから現在価格+付加情報を取得する。"""
        td_sym = _symbol_to_twelvedata(symbol)
        data = self._get_json("/quote", {"symbol": td_sym})

        price = float(data["close"])
        pct = float(data.get("percent_change", 0))
        fiftytwo = data.get("fifty_two_week", {})

        return CurrentPrice(
            price=price,
            timestamp=datetime.now(),
            percent_change=pct,
            rolling_1d_change=float(data.get("change", 0)),
            fifty_two_week_high=float(fiftytwo["high"]) if fiftytwo.get("high") else None,
            fifty_two_week_low=float(fiftytwo["low"]) if fiftytwo.get("low") else None,
            is_market_open=data.get("is_market_open"),
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        period: str = "90d",
        interval: str = "1h",
        price_store: "PriceStore | None" = None,
    ) -> PriceData:
        """time_series エンドポイントから OHLCV を取得する。"""
        td_sym = _symbol_to_twelvedata(symbol)
        td_interval = _interval_to_twelvedata(interval)
        outputsize = _period_to_outputsize(period, interval)

        data = self._get_json("/time_series", {
            "symbol": td_sym,
            "interval": td_interval,
            "outputsize": outputsize,
        })

        values = data.get("values", [])
        if not values:
            raise ValueError(f"No OHLCV data returned for {symbol}")

        rows = []
        for v in values:
            rows.append({
                "datetime": pd.Timestamp(v["datetime"]),
                "Open": float(v["open"]),
                "High": float(v["high"]),
                "Low": float(v["low"]),
                "Close": float(v["close"]),
                "Volume": float(v.get("volume", 0)),
            })

        df = pd.DataFrame(rows)
        df = df.set_index("datetime").sort_index()

        current_price = float(values[0]["close"])  # values[0]が最新

        if price_store is not None:
            price_store.upsert_ohlcv(symbol, df)

        logger.info(f"[TWELVEDATA] {symbol}: fetched {len(df)} bars, latest close={current_price:.5f}")

        return PriceData(
            symbol=symbol,
            df=df,
            current_price=current_price,
            fetched_at=datetime.now(),
        )

    def probe(self) -> bool:
        """API接続確認。成功すればTrue。"""
        try:
            self._get_json("/quote", {"symbol": "USD/JPY"})
            return True
        except Exception as e:
            logger.warning(f"Twelve Data probe failed: {e}")
            return False
