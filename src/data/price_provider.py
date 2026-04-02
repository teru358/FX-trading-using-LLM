"""価格データプロバイダー統合ファサード。

設定に応じて yfinance / Twelve Data を切替え、フォールバックを管理する。
Watch銘柄は常にyfinanceを使用する。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, date
from typing import TYPE_CHECKING

from src.data.price_fetcher import CurrentPrice, PriceData, fetch_current_price, fetch_ohlcv

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.data.price_store import PriceStore

logger = logging.getLogger(__name__)


class PriceProvider:
    """設定に基づいて価格データプロバイダーを選択するファサード。"""

    def __init__(self, config: "AppConfig") -> None:
        self._config = config
        self._provider = config.price_provider.realtime_provider
        self._trade_symbols: set[str] = {
            i.symbol for i in config.tradeable_instruments
        }
        self._td_fetcher = None
        self._daily_count = 0
        self._daily_count_date: date | None = None
        self._daily_limit = config.price_provider.twelvedata.daily_limit

        if self._provider == "twelvedata":
            api_key = os.environ.get("TWELVEDATA_API_KEY", "")
            if api_key:
                from src.data.twelvedata_fetcher import TwelveDataFetcher
                self._td_fetcher = TwelveDataFetcher(api_key=api_key)
            else:
                logger.warning(
                    "TWELVEDATA_API_KEY not set — falling back to yfinance"
                )
                self._provider = "yfinance"

    def _is_trade_pair(self, symbol: str) -> bool:
        return symbol in self._trade_symbols

    def _use_twelvedata(self, symbol: str) -> bool:
        """このシンボルに Twelve Data を使うか判定する。"""
        if self._provider != "twelvedata":
            return False
        if self._td_fetcher is None:
            return False
        if not self._is_trade_pair(symbol):
            return False
        # 日次上限チェック
        today = date.today()
        if self._daily_count_date != today:
            self._daily_count = 0
            self._daily_count_date = today
        if self._daily_count >= self._daily_limit:
            logger.warning("[PROVIDER] Daily limit reached — using yfinance for rest of day")
            return False
        return True

    def _increment_count(self) -> None:
        self._daily_count += 1

    def get_current_price(self, symbol: str) -> CurrentPrice:
        """現在価格を取得する（同期）。Twelve Data有効時は非同期を同期ラップ。"""
        if self._use_twelvedata(symbol):
            try:
                cp = self._td_fetcher.fetch_current_price(symbol)
                self._increment_count()
                return cp
            except Exception as e:
                logger.warning(f"[PROVIDER] Twelve Data failed for {symbol}, fallback to yfinance: {e}")
        return fetch_current_price(symbol)

    def get_ohlcv(
        self,
        symbol: str,
        period: str = "90d",
        interval: str = "1h",
        price_store: "PriceStore | None" = None,
    ) -> PriceData:
        """OHLCV を取得する（同期）。"""
        if self._use_twelvedata(symbol):
            try:
                price_data = self._td_fetcher.fetch_ohlcv(symbol, period, interval, price_store)
                self._increment_count()
                return price_data
            except Exception as e:
                logger.warning(f"[PROVIDER] Twelve Data OHLCV failed for {symbol}, fallback to yfinance: {e}")
        return fetch_ohlcv(symbol, period, interval, price_store)

    def estimate_daily_requests(self) -> int:
        """Twelve Data使用時の日次リクエスト見積もりを返す。yfinance時は0。"""
        if self._provider != "twelvedata":
            return 0
        n_pairs = len(self._trade_symbols)
        interval = self._config.price_monitor.interval_minutes
        monitor_per_hour = 60 // interval
        # :00はOHLCV取得と兼用するため -1
        monitor_only = monitor_per_hour - 1
        ohlcv_per_hour = 1  # 毎時:00
        per_pair_per_hour = monitor_only + ohlcv_per_hour
        daily = n_pairs * per_pair_per_hour * 24
        # 取引判定（3回/日 × n_pairs）
        daily += n_pairs * len(self._config.schedule.run_times)
        return daily

    def status_line(self) -> str:
        """起動パネル用のステータス行を返す。"""
        if self._provider == "yfinance":
            return "yfinance"
        est = self.estimate_daily_requests()
        limit = self._daily_limit
        margin = (limit - est) / limit * 100
        return (
            f"twelvedata (trade pairs) + yfinance (watch)\n"
            f"                 推定 {est} req/日 (上限 {limit})  余裕 {margin:.0f}%"
        )

    def provider_label(self, symbol: str) -> str:
        """シンボルのプロバイダーラベルを返す（LLMプロンプト用）。"""
        if self._use_twelvedata(symbol):
            return "twelvedata, real-time"
        return "yfinance, ~15min delay"
