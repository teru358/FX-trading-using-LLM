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

from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.position_manager import Order, PositionManager

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


def create_broker(trading_mode: str, position_mgr: PositionManager | None = None) -> BrokerAdapter:
    """trading_mode に応じた BrokerAdapter を返すファクトリ関数。

    Args:
        trading_mode: "paper" | "live"
    """
    from src.trading.paper_broker import PaperBrokerAdapter

    if trading_mode == "paper":
        return PaperBrokerAdapter()
    elif trading_mode == "live":
        return LiveBrokerAdapter()
    else:
        raise ValueError(f"Unknown trading_mode: {trading_mode!r}. Use 'paper' or 'live'.")
