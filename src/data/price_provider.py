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
    """設定に基づいて価格データプロバイダーを選択するファサード。

    Twelve Data有効時:
      - trade銘柄: 常にTwelve Data（リアルタイムFX）
      - watch銘柄: td_symbols に含まれればTwelve Data、それ以外はyfinance
      - price_monitor: use_for_monitor設定に従う
    """

    def __init__(self, config: "AppConfig") -> None:
        self._config = config
        self._provider = config.price_provider.realtime_provider
        self._trade_symbols: set[str] = {
            i.symbol for i in config.tradeable_instruments
        }
        # Twelve Data対応シンボル（trade + 設定で指定されたwatch銘柄）
        self._td_symbols: set[str] = set(self._trade_symbols)
        for sym in config.price_provider.twelvedata.watch_symbols:
            self._td_symbols.add(sym)
        self._use_for_monitor = config.price_provider.twelvedata.use_for_monitor
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

    def _use_twelvedata(self, symbol: str, is_monitor: bool = False) -> bool:
        """このシンボルに Twelve Data を使うか判定する。"""
        if self._provider != "twelvedata":
            return False
        if self._td_fetcher is None:
            return False
        # monitorからの呼び出しはuse_for_monitor設定に従う
        if is_monitor and not self._use_for_monitor:
            return False
        if symbol not in self._td_symbols:
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

    def get_current_price(self, symbol: str, is_monitor: bool = False) -> CurrentPrice:
        """現在価格を取得する（同期）。Twelve Data有効時は非同期を同期ラップ。"""
        if self._use_twelvedata(symbol, is_monitor=is_monitor):
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
        n_trade = len(self._trade_symbols)
        n_watch_td = len(self._td_symbols - self._trade_symbols)
        interval = self._config.price_monitor.interval_minutes
        monitor_per_hour = 60 // interval

        # price_monitor (trade銘柄のみ、use_for_monitor設定に従う)
        if self._use_for_monitor:
            # :00はOHLCV取得と兼用するため -1
            monitor_only = monitor_per_hour - 1
            daily = n_trade * monitor_only * 24
        else:
            daily = 0

        # テクニカル分析 OHLCV (trade + watch TD銘柄、毎時:00)
        daily += (n_trade + n_watch_td) * 24
        # 取引判定 (trade銘柄のみ)
        daily += n_trade * len(self._config.schedule.run_times)
        return daily

    def status_line(self) -> str:
        """起動パネル用のステータス行を返す。"""
        if self._provider == "yfinance":
            return "yfinance"
        n_watch_td = len(self._td_symbols - self._trade_symbols)
        est = self.estimate_daily_requests()
        limit = self._daily_limit
        margin = (limit - est) / limit * 100
        td_scope = "trade"
        if n_watch_td > 0:
            td_scope += f" + {n_watch_td} watch"
        monitor_label = "TD" if self._use_for_monitor else "yfinance"
        return (
            f"twelvedata ({td_scope}) + yfinance (残watch)  monitor={monitor_label}\n"
            f"                 推定 {est} req/日 (上限 {limit})  余裕 {margin:.0f}%"
        )

    def provider_label(self, symbol: str) -> str:
        """シンボルのプロバイダーラベルを返す（LLMプロンプト用）。"""
        if self._use_twelvedata(symbol):
            return "twelvedata, real-time"
        return "yfinance, ~15min delay"
