from __future__ import annotations

"""OANDA REST API v20 を使った本取引ブローカーアダプター。

# セットアップ
1. OANDA口座を開設し、APIキーを取得する
   https://developer.oanda.com/rest-live-v20/introduction/

2. .env に以下を設定する:
   OANDA_API_KEY=your_api_key_here
   OANDA_ACCOUNT_ID=your_account_id_here
   OANDA_ENVIRONMENT=practice   # "practice" | "live"

3. pyproject.toml に依存を追加:
   uv add oandapyV20

# 注意事項
- trading_mode: "live" に設定すると実際の注文が発注される
- 本番移行前に必ず practice 環境 (trading_mode: "practice_live") でテストすること
- ロットサイズ (position_size) の単位は通貨ペアによって異なる（OANDA: units）
"""

import logging
import os
from typing import TYPE_CHECKING

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.position_manager import Order, PositionManager

if TYPE_CHECKING:
    from src.notifications.notifier import NotifierAdapter

logger = logging.getLogger(__name__)

# OANDA APIエンドポイント
_API_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# yfinance シンボル → OANDA instrument 変換テーブル
_SYMBOL_TO_INSTRUMENT = {
    "USDJPY=X": "USD_JPY",
    "EURUSD=X": "EUR_USD",
    "GBPUSD=X": "GBP_USD",
    "AUDUSD=X": "AUD_USD",
    "USDCHF=X": "USD_CHF",
}


class LiveBrokerAdapter(BrokerAdapter):
    """OANDA REST API v20 経由の本取引実装。

    TODO: 実装が必要なメソッドは NotImplementedError を送出する。
    本番移行前に各メソッドを実装し、practice 環境でE2Eテストを行うこと。
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("OANDA_API_KEY", "")
        self._account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        env = os.environ.get("OANDA_ENVIRONMENT", "practice")
        self._base_url = _API_URLS.get(env, _API_URLS["practice"])

        if not self._api_key or not self._account_id:
            raise EnvironmentError(
                "OANDA_API_KEY と OANDA_ACCOUNT_ID を .env に設定してください。"
            )
        logger.info(f"[LIVE] LiveBrokerAdapter initialized (env={env}, url={self._base_url})")

    def _instrument(self, yf_symbol: str) -> str:
        """yfinanceシンボルをOANDA instrument名に変換する。"""
        inst = _SYMBOL_TO_INSTRUMENT.get(yf_symbol)
        if inst is None:
            raise ValueError(f"OANDA instrument mapping not found for symbol: {yf_symbol}")
        return inst

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        """OANDA Market Order を発注する。

        実装手順:
        1. _instrument() でシンボル変換
        2. signal.action ("buy"/"sell") → units（買い: +, 売り: -）に変換
        3. POST /v3/accounts/{id}/orders でMarket Order発注
           payload = {
               "order": {
                   "type": "MARKET",
                   "instrument": instrument,
                   "units": str(units),
                   "stopLossOnFill": {"price": str(signal.stop_loss)},
                   "takeProfitOnFill": {"price": str(signal.take_profit)},
               }
           }
        4. レスポンスの orderFillTransaction.id を order_id として保存
        5. position_mgr.open_position(order) でローカル状態を更新

        参考: https://developer.oanda.com/rest-live-v20/orders-ep/
        """
        raise NotImplementedError(
            "LiveBrokerAdapter.execute_signal() is not yet implemented. "
            "See docstring for implementation guide."
        )

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        """OANDA側でSL/TP決済済みのポジションをローカル状態に反映する。

        実装手順:
        1. GET /v3/accounts/{id}/trades でOANDA側のオープンポジション一覧取得
        2. ローカルの open_positions と照合
        3. OANDA側でクローズ済み（state="CLOSED"）のものを position_mgr.close_position() で反映
        4. ローカルにあってOANDAにない → SL/TPで約定済みとみなす

        代替: OANDAのtransaction streamを購読してリアルタイム反映する方式もある
        参考: https://developer.oanda.com/rest-live-v20/trades-ep/

        NOTE: ペーパートレードと異なり、ブローカー側がSL/TPを管理するため
              yfinanceの価格での手動チェックは不要になる。
        """
        raise NotImplementedError(
            "LiveBrokerAdapter.check_and_close_positions() is not yet implemented. "
            "See docstring for implementation guide."
        )


def create_broker(
    trading_mode: str,
    position_mgr: PositionManager | None = None,
    *,
    max_positions_per_pair: int = 2,
    scale_in_enabled: bool = False,
    scale_in_conf_margin: float = 0.05,
    scale_in_score_margin: float = 0.05,
    manual_position_mgr: "PositionManager | None" = None,
    notifier: "NotifierAdapter | None" = None,
    drawdown_kill_switch_enabled: bool = False,
    drawdown_kill_switch_max_pct: float = 0.10,
    drawdown_kill_switch_lookback_days: int = 0,
    # ── mt5_bridge / shadow 用 (Phase 3a) ──
    mt5_bridge_url: str = "",
    mt5_lot_size_units: int = 100_000,
    mt5_magic_number: int = 12345,
    mt5_order_timeout_seconds: float = 10.0,
    shadow_log_path: str = "data/state/shadow_trades.jsonl",
    shadow_observer_state_dir: str = "data/shadow_state",
    initial_balance: float = 100_000.0,
) -> BrokerAdapter:
    """trading_mode に応じた BrokerAdapter を返すファクトリ関数。

    Args:
        trading_mode: "paper" | "live" | "signal" | "mt5_bridge" | "shadow"
        max_positions_per_pair: 1ペアあたりの最大ポジション数 (scale-in上限)。
        scale_in_enabled: スケールインを許可するか。
        scale_in_conf_margin: スケールイン時に要求する追加信頼度マージン。
        scale_in_score_margin: スケールイン時に要求する追加スコアマージン。
        manual_position_mgr: "signal" モード時に必須。manual ポジション管理用。
        notifier: "signal" モード時に必須。通知アダプター。
        drawdown_kill_switch_*: 新規エントリーの DD kill switch 設定。
        mt5_bridge_url: "mt5_bridge" or "shadow" 時に必須。
        shadow_*: "shadow" 時に observer 専用 state_store と比較ログの場所を指定。
        initial_balance: shadow モードで observer 専用 PositionManager の初期残高。
    """
    from src.trading.paper_broker import PaperBrokerAdapter

    if trading_mode == "paper":
        return PaperBrokerAdapter(
            max_positions_per_pair=max_positions_per_pair,
            scale_in_enabled=scale_in_enabled,
            scale_in_conf_margin=scale_in_conf_margin,
            scale_in_score_margin=scale_in_score_margin,
            drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
            drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
            drawdown_kill_switch_lookback_days=drawdown_kill_switch_lookback_days,
        )
    elif trading_mode == "live":
        return LiveBrokerAdapter()
    elif trading_mode == "signal":
        if manual_position_mgr is None:
            raise ValueError(
                "signal モードでは manual_position_mgr が必須です。"
            )
        if notifier is None:
            raise ValueError(
                "signal モードでは notifier が必須です。"
            )
        from src.trading.signal_broker import SignalBrokerAdapter
        return SignalBrokerAdapter(
            manual_position_mgr=manual_position_mgr,
            notifier=notifier,
            max_positions_per_pair=max_positions_per_pair,
            scale_in_enabled=scale_in_enabled,
            scale_in_conf_margin=scale_in_conf_margin,
            scale_in_score_margin=scale_in_score_margin,
            drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
            drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
            drawdown_kill_switch_lookback_days=drawdown_kill_switch_lookback_days,
        )
    elif trading_mode == "mt5_bridge":
        from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
        if not mt5_bridge_url:
            raise ValueError(
                "trading_mode='mt5_bridge' requires mt5_bridge.bridge_url"
            )
        return Mt5BridgeBrokerAdapter(
            bridge_url=mt5_bridge_url,
            request_timeout_seconds=mt5_order_timeout_seconds,
            lot_size_units=mt5_lot_size_units,
            magic_number=mt5_magic_number,
            max_positions_per_pair=max_positions_per_pair,
            scale_in_enabled=scale_in_enabled,
            scale_in_conf_margin=scale_in_conf_margin,
            scale_in_score_margin=scale_in_score_margin,
            drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
            drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
            drawdown_kill_switch_lookback_days=drawdown_kill_switch_lookback_days,
        )
    elif trading_mode == "shadow":
        from pathlib import Path
        from src.persistence.state_store import StateStore
        from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
        from src.trading.shadow_broker import ShadowBrokerAdapter

        if not mt5_bridge_url:
            raise ValueError(
                "trading_mode='shadow' requires mt5_bridge.bridge_url"
            )

        primary = PaperBrokerAdapter(
            max_positions_per_pair=max_positions_per_pair,
            scale_in_enabled=scale_in_enabled,
            scale_in_conf_margin=scale_in_conf_margin,
            scale_in_score_margin=scale_in_score_margin,
            drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
            drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
            drawdown_kill_switch_lookback_days=drawdown_kill_switch_lookback_days,
        )
        observer = Mt5BridgeBrokerAdapter(
            bridge_url=mt5_bridge_url,
            request_timeout_seconds=mt5_order_timeout_seconds,
            lot_size_units=mt5_lot_size_units,
            magic_number=mt5_magic_number,
            max_positions_per_pair=max_positions_per_pair,
            scale_in_enabled=scale_in_enabled,
            scale_in_conf_margin=scale_in_conf_margin,
            scale_in_score_margin=scale_in_score_margin,
            drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
            drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
            drawdown_kill_switch_lookback_days=drawdown_kill_switch_lookback_days,
        )
        # observer 専用 state_store + position_mgr (paper の state を汚染しない)
        obs_store = StateStore(Path(shadow_observer_state_dir))
        obs_pm = PositionManager(
            obs_store, initial_balance, context="ShadowObserver",
        )
        return ShadowBrokerAdapter(
            primary=primary, observer=observer,
            observer_position_mgr=obs_pm,
            comparison_log_path=Path(shadow_log_path),
        )
    else:
        raise ValueError(
            f"Unknown trading_mode: {trading_mode!r}. "
            "Use 'paper', 'live', 'signal', 'mt5_bridge', or 'shadow'."
        )
