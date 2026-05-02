"""価格データプロバイダー統合ファサード。

trade ペアは MT5 → Twelve Data → yfinance を順に試行し、失敗時自動降格する
(Phase 3b)。watch ペアは Twelve Data → yfinance のまま (MT5 でブローカー
依存性あり、本タスクのスコープ外)。

state 遷移時のみ通知発火。bridge unreachable と MT5 disconnected の区別は
/health レスポンスの mt5_connected を見る。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, Mt5UnreachableError
from src.data.price_fetcher import CurrentPrice, PriceData, fetch_current_price, fetch_ohlcv
from src.data.provider_health_tracker import ProviderHealthTracker

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

    def __init__(
        self, config: "AppConfig", *,
        health_tracker: ProviderHealthTracker | None = None,
        notifier=None,
    ) -> None:
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

        # Phase 3b: MT5 chain
        self._mt5_fetcher: Mt5OhlcvFetcher | None = None
        if config.mt5_bridge.bridge_url:
            self._mt5_fetcher = Mt5OhlcvFetcher(
                bridge_url=config.mt5_bridge.bridge_url,
                request_timeout=config.mt5_bridge.request_timeout_seconds,
            )
        self._health_tracker = health_tracker or ProviderHealthTracker(
            failure_window_sec=config.mt5_bridge.fallback.failure_window_sec,
            failure_threshold=config.mt5_bridge.fallback.failure_threshold,
        )
        self._notifier = notifier
        self._active_provider: dict[str, str] = {}        # symbol → provider name
        self._last_health_check_at: datetime | None = None
        self._health_check_interval = timedelta(
            minutes=config.mt5_bridge.fallback.heartbeat_interval_degraded_min,
        )

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
        """OHLCV を取得する（同期）。

        trade ペア: MT5 → TD → yfinance の順で試行。
        watch ペア: TD → yfinance (既存動作維持)。
        """
        # degraded 中なら 15min 毎に /health で復帰確認
        self._maybe_check_bridge_health()

        prev = self._active_provider.get(symbol, "mt5")

        # Phase 3b: trade ペアは MT5 を最優先
        if self._is_trade_pair(symbol) and self._mt5_fetcher is not None:
            try:
                data = self._mt5_fetcher.fetch(
                    symbol, period=period, interval=interval, price_store=price_store,
                )
                if self._health_tracker.record_success(datetime.now()):
                    self._notify_recovery(prev, "mt5")
                self._record_provider(symbol, prev, "mt5")
                return data
            except Mt5UnreachableError as e:
                if self._health_tracker.record_failure(datetime.now()):
                    self._notify_degraded(reason=str(e))
                logger.warning(f"[PROVIDER] {symbol} MT5 fetch failed: {e}")
            except Exception as e:
                logger.error(f"[PROVIDER] {symbol} MT5 unexpected error: {e}")

        # Twelve Data
        if self._use_twelvedata(symbol):
            try:
                price_data = self._td_fetcher.fetch_ohlcv(symbol, period, interval, price_store)
                self._increment_count()
                self._record_provider(symbol, prev, "twelvedata")
                return price_data
            except Exception as e:
                logger.warning(
                    f"[PROVIDER] Twelve Data OHLCV failed for {symbol}, "
                    f"fallback to yfinance: {e}"
                )

        # yfinance (最終)
        data = fetch_ohlcv(symbol, period, interval, price_store)
        self._record_provider(symbol, prev, "yfinance")
        return data

    def _record_provider(self, symbol: str, prev: str, current: str) -> None:
        self._active_provider[symbol] = current
        if prev != current:
            logger.warning(f"[PROVIDER] {symbol}: {prev} → {current}")

    def _maybe_check_bridge_health(self) -> None:
        """degraded 中で 15 分以上経過していれば /health で復帰確認。

        - 失敗時は failure 加算しない (degraded 中の確認失敗は当然なのでカウンタを汚さない)
        - 取引時間外で get_ohlcv が呼ばれない期間は復帰検知が止まる (許容)
        """
        if not self._health_tracker.is_degraded:
            return
        if self._mt5_fetcher is None or not self._config.mt5_bridge.bridge_url:
            return
        now = datetime.now()
        if self._last_health_check_at is not None:
            if now - self._last_health_check_at < self._health_check_interval:
                return
        self._last_health_check_at = now

        try:
            resp = httpx.get(
                f"{self._config.mt5_bridge.bridge_url.rstrip('/')}/health",
                timeout=5.0,
            )
            resp.raise_for_status()
            if resp.json().get("mt5_connected"):
                if self._health_tracker.record_success(now):
                    self._notify_recovery(prev="twelvedata", current="mt5")
        except (httpx.HTTPError, ValueError):
            # 復帰失敗 → 状態維持 (failure 加算はしない)
            pass

    def _notify_degraded(self, reason: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.send_embed(
                title="⚠️ MT5 ブリッジ → フォールバック発動",
                description=(
                    f"原因: {reason}\n"
                    f"代替: Twelve Data → yfinance の順で取得\n"
                    f"自動復帰確認: 15 分毎に /health 確認中"
                ),
                color=0xF39C12,
            )
        except Exception as e:
            logger.warning(f"[PROVIDER] degraded notification failed: {e}")

    def _notify_recovery(self, prev: str, current: str) -> None:
        if self._notifier is None or prev == current:
            return
        try:
            self._notifier.send_embed(
                title="✅ MT5 ブリッジ 復帰",
                description=(
                    f"自動復旧確認、現在は MT5 経由で取得中\n"
                    f"前回プロバイダ: {prev}"
                ),
                color=0x2ECC71,
            )
        except Exception as e:
            logger.warning(f"[PROVIDER] recovery notification failed: {e}")

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
