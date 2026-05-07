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
- mode: "live" + live_broker: "oanda" に設定すると実際の OANDA 注文が発注される
- 本番移行前に必ず practice 環境 (providers/oanda.yaml の environment="practice") でテストすること
- mode: "live_test" は paper + observer の同時実行で、実発注はしない (検証用)
- ロットサイズ (position_size) の単位は通貨ペアによって異なる（OANDA: units）
"""

import logging
import os
from typing import TYPE_CHECKING, Literal

from src.persistence import balance_snapshot
from src.persistence.balance_snapshot import OBSERVER_DEFAULT, BalanceSnapshot
from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.position_manager import Order, PositionManager

if TYPE_CHECKING:
    from pathlib import Path

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
    mode: Literal["paper", "live", "live_test"],
    live_broker: Literal["mt5", "oanda"] | None,
    *,
    max_positions_per_pair: int = 2,
    scale_in_enabled: bool = False,
    scale_in_conf_margin: float = 0.05,
    scale_in_score_margin: float = 0.05,
    notifier: "NotifierAdapter | None" = None,
    state_dir: "Path | None" = None,
    drawdown_kill_switch_enabled: bool = False,
    drawdown_kill_switch_max_pct: float = 0.10,
    # ── live_broker=mt5 のとき必須 ──
    mt5_bridge_url: str = "",
    mt5_lot_size_units: int = 100_000,
    mt5_magic_number: int = 12345,
    mt5_order_timeout_seconds: float = 10.0,
    mt5_consecutive_unreachable_threshold: int = 3,
    mt5_consecutive_reject_threshold: int = 3,
    # ── mode=live_test のとき必須 ──
    live_test_log_path: str = "data/state/shadow_trades.jsonl",
    live_test_observer_state_dir: str = "data/shadow_state",
) -> BrokerAdapter:
    """mode + live_broker に応じた BrokerAdapter を返すファクトリ関数。

    Args:
        mode: "paper" | "live" | "live_test"
        live_broker: "mt5" | "oanda" | None — mode in {live, live_test} で必須
        max_positions_per_pair: 1ペアあたりの最大ポジション数 (scale-in上限)。
        scale_in_enabled: スケールインを許可するか。
        scale_in_conf_margin: スケールイン時に要求する追加信頼度マージン。
        scale_in_score_margin: スケールイン時に要求する追加スコアマージン。
        notifier: 通知アダプター。
        drawdown_kill_switch_*: 新規エントリーの DD kill switch 設定。
        mt5_bridge_url: live_broker="mt5" のとき必須。
        live_test_*: mode="live_test" のとき observer 専用 state_store と比較ログの場所を指定。
    """
    from src.trading.paper_broker import PaperBrokerAdapter

    paper_kwargs = dict(
        max_positions_per_pair=max_positions_per_pair,
        scale_in_enabled=scale_in_enabled,
        scale_in_conf_margin=scale_in_conf_margin,
        scale_in_score_margin=scale_in_score_margin,
        drawdown_kill_switch_enabled=drawdown_kill_switch_enabled,
        drawdown_kill_switch_max_pct=drawdown_kill_switch_max_pct,
    )

    if mode == "paper":
        return PaperBrokerAdapter(**paper_kwargs)

    if mode == "live":
        if live_broker == "oanda":
            return LiveBrokerAdapter()
        if live_broker == "mt5":
            from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
            if not mt5_bridge_url:
                raise ValueError(
                    "mode='live' + live_broker='mt5' requires mt5_bridge_url"
                )
            return Mt5BridgeBrokerAdapter(
                bridge_url=mt5_bridge_url,
                request_timeout_seconds=mt5_order_timeout_seconds,
                lot_size_units=mt5_lot_size_units,
                magic_number=mt5_magic_number,
                notifier=notifier,
                consecutive_unreachable_threshold=mt5_consecutive_unreachable_threshold,
                consecutive_reject_threshold=mt5_consecutive_reject_threshold,
                state_dir=state_dir,
                **paper_kwargs,
            )
        raise ValueError(
            f"mode='live' requires live_broker in ('mt5', 'oanda'), got {live_broker!r}"
        )

    if mode == "live_test":
        if live_broker != "mt5":
            raise ValueError(
                f"mode='live_test' requires live_broker='mt5', got {live_broker!r}"
            )
        from pathlib import Path
        from src.persistence.state_store import StateStore
        from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter
        from src.trading.shadow_broker import ShadowBrokerAdapter

        if not mt5_bridge_url:
            raise ValueError(
                "mode='live_test' requires mt5_bridge_url"
            )
        primary = PaperBrokerAdapter(**paper_kwargs)
        observer = Mt5BridgeBrokerAdapter(
            bridge_url=mt5_bridge_url,
            request_timeout_seconds=mt5_order_timeout_seconds,
            lot_size_units=mt5_lot_size_units,
            magic_number=mt5_magic_number,
            notifier=notifier,
            consecutive_unreachable_threshold=mt5_consecutive_unreachable_threshold,
            consecutive_reject_threshold=mt5_consecutive_reject_threshold,
            state_dir=state_dir,
            **paper_kwargs,
        )
        obs_state_dir = Path(live_test_observer_state_dir)
        obs_state_dir.mkdir(parents=True, exist_ok=True)
        # observer 用 balance.json は state_dir 内に独立 (主 balance.json と分離)。
        # Phase 3c 暫定: balance が OBSERVER_DEFAULT から drift していたら毎回リセット。
        # observer balance は情報用のみで、取引判定には影響しない (Phase 4 で再設計)。
        obs_snap = balance_snapshot.read(obs_state_dir)
        if obs_snap.balance != OBSERVER_DEFAULT or obs_snap.source != "paper":
            from datetime import datetime, timezone
            balance_snapshot.write(obs_state_dir, BalanceSnapshot(
                balance=OBSERVER_DEFAULT, deposit=OBSERVER_DEFAULT,
                peak_balance=OBSERVER_DEFAULT, source="paper",
                fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            ))
        obs_store = StateStore(obs_state_dir)
        obs_pm = PositionManager(obs_store, context="LiveTestObserver")
        return ShadowBrokerAdapter(
            primary=primary, observer=observer,
            observer_position_mgr=obs_pm,
            comparison_log_path=Path(live_test_log_path),
        )

    raise ValueError(
        f"Unknown mode: {mode!r}. Use 'paper', 'live', or 'live_test'."
    )


def build_close_broker(config) -> BrokerAdapter:
    """close-only 用途で broker を構築する (新規発注は想定しない)。

    `/close/{pair}` API、CLI close、price_monitor emergency_stop など、
    halt 中でも実 MT5 ポジを閉じる必要がある経路から呼ばれる。

    paper モードでは PaperBrokerAdapter.close_position の default 実装が
    pm.close_position に委譲するので、内部 state 更新だけで済む。

    live/live_test + MT5 では Mt5BridgeBrokerAdapter.close_position が
    実 MT5 サーバーへ close 指令を送る。
    """
    mt5_cfg = getattr(config.providers, "mt5", None)
    return create_broker(
        config.mode,
        config.live_broker,
        notifier=None,    # close 経路は呼出側で個別に通知する
        state_dir=config.state_dir,
        mt5_bridge_url=(mt5_cfg.bridge_url if mt5_cfg else ""),
        mt5_lot_size_units=(mt5_cfg.lot_size_units if mt5_cfg else 100_000),
        mt5_magic_number=(mt5_cfg.magic_number if mt5_cfg else 12345),
        mt5_order_timeout_seconds=(
            getattr(mt5_cfg, "order_request_timeout_seconds", 10.0) if mt5_cfg else 10.0
        ),
        mt5_consecutive_unreachable_threshold=(
            getattr(mt5_cfg, "consecutive_unreachable_threshold", 3) if mt5_cfg else 3
        ),
        mt5_consecutive_reject_threshold=(
            getattr(mt5_cfg, "consecutive_reject_threshold", 3) if mt5_cfg else 3
        ),
        live_test_log_path=(
            getattr(mt5_cfg, "shadow_log_path", "data/state/shadow_trades.jsonl")
            if mt5_cfg else "data/state/shadow_trades.jsonl"
        ),
        live_test_observer_state_dir=(
            getattr(mt5_cfg, "shadow_observer_state_dir", "data/shadow_state")
            if mt5_cfg else "data/shadow_state"
        ),
    )
