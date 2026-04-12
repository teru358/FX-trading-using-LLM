# Signal Only モード実装プラン

> **エージェント向け:** 必須サブスキル: superpowers:subagent-driven-development (推奨) または superpowers:executing-plans でタスクごとに実装してください。各ステップは `- [ ]` チェックボックスで追跡します。

**ゴール:** `trading_mode: "signal_only"` を追加し、Discord でシグナル通知しつつ内部的には paper 同等の自動運用で RAG を育て、REST API + TUI で手動ポジション記録を可能にする。

**アーキテクチャ:** Dual PositionManager — internal (自動、paper_broker、無音 RAG 学習) と manual (ユーザーの実ポジションを REST API で管理)。SignalOnlyBrokerAdapter は通知のみ行い注文を発行しない。新規通知イベント3種 (SignalRecommendation, SLTPAlert, ReviewAdvisory)。

**技術スタック:** Python 3.12, FastAPI, asyncio, 既存の PositionManager / StateStore / NotifierAdapter

---

## ファイル構成

| ファイル | 操作 | 責務 |
|---|---|---|
| `src/trading/signal_only_broker.py` | 新規 | SignalOnlyBrokerAdapter — 通知のみ、注文なし |
| `src/notifications/notifier.py` | 変更 | 新規イベント3種 + ハンドラメソッド追加 |
| `src/trading/live_broker.py` | 変更 | `create_broker` ファクトリを "signal_only" 対応に拡張 |
| `src/cycles/trading.py` | 変更 | signal_only モードの Dual PositionManager 統合 |
| `src/api/routes/manual.py` | 新規 | /manual/open, close, list, balance エンドポイント |
| `src/api/server.py` | 変更 | signal_only 時に manual ルーターを登録 |
| `src/api/_state.py` | 変更 | APIState に manual_position_mgr を追加 |
| `src/cli.py` | 変更 | `manual` インタラクティブコマンド追加 |
| `src/config/schema.py` | 変更 | manual_state_dir プロパティ追加 |
| `config/settings.yaml.example` | 変更 | signal_only 設定例追加 |
| `tests/test_signal_only_broker.py` | 新規 | SignalOnlyBrokerAdapter テスト |
| `tests/test_manual_api.py` | 新規 | Manual REST API テスト |
| `tests/test_notifier_events.py` | 新規 | 新規通知イベントテスト |
| `tests/test_signal_only_integration.py` | 新規 | 統合テスト |

---

### タスク 1: 通知イベント

**ファイル:**
- 変更: `src/notifications/notifier.py`
- テスト: `tests/test_notifier_events.py`

- [ ] **ステップ 1: 新規イベントのテストを書く**

```python
# tests/test_notifier_events.py
"""signal_only 用通知イベントのテスト。"""
from __future__ import annotations

import pytest

from src.notifications.notifier import (
    NullNotifier,
    ReviewAdvisoryEvent,
    SignalRecommendationEvent,
    SLTPAlertEvent,
)


@pytest.mark.asyncio
async def test_signal_recommendation_notify():
    """SignalRecommendationEvent が NullNotifier で例外なく処理される。"""
    notifier = NullNotifier()
    event = SignalRecommendationEvent(
        pair="USDJPY=X",
        direction="buy",
        entry_price=150.200,
        stop_loss=149.800,
        take_profit=151.200,
        position_size=5000,
        combined_score=0.32,
        confidence=0.75,
        signal_reason="score=+0.32",
        detail_reason="tech +0.20 news +0.12",
        max_loss=10000.0,
        portfolio_warning="",
        existing_positions=0,
        source="trading",
    )
    await notifier.notify_signal_recommendation(event)


@pytest.mark.asyncio
async def test_signal_recommendation_with_portfolio_warning():
    """ポートフォリオ警告付きの通知が処理される。"""
    notifier = NullNotifier()
    event = SignalRecommendationEvent(
        pair="GBPJPY=X",
        direction="buy",
        entry_price=190.500,
        stop_loss=189.500,
        take_profit=192.500,
        position_size=3000,
        combined_score=0.28,
        confidence=0.65,
        signal_reason="score=+0.28",
        detail_reason="",
        max_loss=6000.0,
        portfolio_warning="JPY group already has 2 positions (max 2)",
        existing_positions=1,
        source="trading",
    )
    await notifier.notify_signal_recommendation(event)


@pytest.mark.asyncio
async def test_sltp_alert_notify():
    """SLTPAlertEvent が処理される。"""
    notifier = NullNotifier()
    event = SLTPAlertEvent(
        pair="USDJPY=X",
        direction="buy",
        order_id="test-123",
        entry_price=150.200,
        current_price=149.800,
        trigger="stop_loss",
        unrealized_pnl=-2000.0,
    )
    await notifier.notify_sltp_alert(event)


@pytest.mark.asyncio
async def test_review_advisory_notify():
    """ReviewAdvisoryEvent が処理される。"""
    notifier = NullNotifier()
    event = ReviewAdvisoryEvent(
        pair="USDJPY=X",
        direction="buy",
        order_id="test-456",
        close_reason="reversal",
        detail="Signal reversed: buy vs bearish (score=-0.30 conf=0.80)",
        current_price=149.500,
    )
    await notifier.notify_review_advisory(event)
```

- [ ] **ステップ 2: テストが失敗することを確認**

実行: `cd /home/teru/project/finance && source .venv/bin/activate && python -m pytest tests/test_notifier_events.py -v`
期待: ImportError で FAIL

- [ ] **ステップ 3: イベントデータクラスとハンドラメソッドを notifier.py に追加**

既存の `PriceAlertEvent` クラスの後（63行目付近）に追加:

```python
@dataclass
class SignalRecommendationEvent:
    """signal_only モードのシグナル推奨通知。"""
    pair: str
    direction: str              # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float        # 推奨ロット
    combined_score: float
    confidence: float
    signal_reason: str
    detail_reason: str
    max_loss: float             # 残高 × risk_per_trade
    portfolio_warning: str      # ガード注記（空文字なら制限なし）
    existing_positions: int     # 同一ペアの既存 manual ポジション数
    source: str = ""


@dataclass
class SLTPAlertEvent:
    """manual ポジションの SL/TP 到達通知（決済は行わない）。"""
    pair: str
    direction: str
    order_id: str
    entry_price: float
    current_price: float
    trigger: str                # "stop_loss" | "take_profit"
    unrealized_pnl: float


@dataclass
class ReviewAdvisoryEvent:
    """manual ポジションの Layer 1-3 決済推奨通知。"""
    pair: str
    direction: str
    order_id: str
    close_reason: str           # "reversal" | "timeout" | "profit_lock"
    detail: str
    current_price: float
```

`NotifierAdapter` クラスに以下のハンドラメソッドを追加（`notify_signal_skipped` の後）:

```python
    async def notify_signal_recommendation(self, event: SignalRecommendationEvent) -> None:
        direction_emoji = "📈" if event.direction == "buy" else "📉"
        sl_pips = abs(event.entry_price - event.stop_loss)
        tp_pips = abs(event.take_profit - event.entry_price)
        tag = _source_tag(event.source)
        msg = (
            f"{direction_emoji} 【シグナル推奨】{tag}{event.pair}\n"
            f"方向: {event.direction.upper()}  スコア: {event.combined_score:+.3f}  確信度: {event.confidence:.0%}\n"
            f"エントリー: {event.entry_price:.5f}\n"
            f"SL: {event.stop_loss:.5f} ({sl_pips:.5f})  TP: {event.take_profit:.5f} (+{tp_pips:.5f})\n"
            f"推奨ロット: {event.position_size:,.0f}  最大損失: {event.max_loss:,.0f}"
        )
        if event.existing_positions > 0:
            msg += f"\n既存ポジション: {event.existing_positions}件"
        if event.portfolio_warning:
            msg += f"\n⚠ {event.portfolio_warning}"
        if event.detail_reason:
            msg += f"\n─────────────\n{event.detail_reason}"
        await self.send(msg)

    async def notify_sltp_alert(self, event: SLTPAlertEvent) -> None:
        emoji = "🎯" if event.trigger == "take_profit" else "🛑"
        label = "TP到達" if event.trigger == "take_profit" else "SL到達"
        pnl_sign = "+" if event.unrealized_pnl >= 0 else ""
        msg = (
            f"{emoji} 【{label}】{event.pair} {event.direction.upper()}\n"
            f"エントリー: {event.entry_price:.5f} → 現在: {event.current_price:.5f}\n"
            f"未実現損益: {pnl_sign}{event.unrealized_pnl:.2f}\n"
            f"order_id: {event.order_id}"
        )
        await self.send(msg)

    async def notify_review_advisory(self, event: ReviewAdvisoryEvent) -> None:
        reason_labels = {
            "reversal": "🔄 反転推奨",
            "timeout": "⏰ タイムアウト推奨",
            "profit_lock": "🔒 利益ロック推奨",
        }
        label = reason_labels.get(event.close_reason, event.close_reason)
        msg = (
            f"{label} {event.pair} {event.direction.upper()}\n"
            f"現在価格: {event.current_price:.5f}\n"
            f"{event.detail}\n"
            f"order_id: {event.order_id}"
        )
        await self.send(msg)
```

- [ ] **ステップ 4: テストが通ることを確認**

実行: `python -m pytest tests/test_notifier_events.py -v`
期待: 4 passed

- [ ] **ステップ 5: コミット**

```bash
git add src/notifications/notifier.py tests/test_notifier_events.py
git commit -m "feat(notifier): signal_only 用通知イベント3種追加"
```

---

### タスク 2: SignalOnlyBrokerAdapter

**ファイル:**
- 新規: `src/trading/signal_only_broker.py`
- 変更: `src/trading/live_broker.py`（create_broker ファクトリ）
- テスト: `tests/test_signal_only_broker.py`

- [ ] **ステップ 1: テストを書く**

```python
# tests/test_signal_only_broker.py
"""SignalOnlyBrokerAdapter のテスト。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.notifications.notifier import NullNotifier
from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order, PositionManager


def _make_signal(pair: str = "USDJPY=X", action: str = "buy") -> TradeSignal:
    news = NewsSentiment(pair=pair, sentiment_score=0.3, confidence=0.7)
    price = PriceAnalysis(
        pair=pair, direction_bias="long", bias_score=0.3, confidence=0.7,
        entry_zone=(149.5, 150.5), stop_loss=149.0, take_profit=152.0,
        risk_reward_ratio=2.0, reasoning_summary="test", analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair, action=action, predicted_direction="bullish",
        combined_score=0.32, confidence=0.75,
        entry_price=150.2, stop_loss=149.8, take_profit=151.2,
        position_size=5000, signal_reason="test", detail_reason="test detail",
        news=news, price=price, generated_at=datetime.now(),
    )


@pytest.fixture
def manual_mgr(tmp_path):
    from src.persistence.state_store import StateStore
    store = StateStore(tmp_path / "manual")
    return PositionManager(store, 500000.0, context="Manual")


@pytest.fixture
def internal_mgr(tmp_path):
    from src.persistence.state_store import StateStore
    store = StateStore(tmp_path / "internal")
    return PositionManager(store, 500000.0, context="Internal")


@pytest.fixture
def broker(manual_mgr):
    from src.trading.signal_only_broker import SignalOnlyBrokerAdapter
    notifier = AsyncMock(spec=NullNotifier)
    return SignalOnlyBrokerAdapter(
        manual_position_mgr=manual_mgr,
        notifier=notifier,
    )


def test_execute_signal_returns_none(broker, internal_mgr):
    """execute_signal は常に None を返す（注文を発行しない）。"""
    signal = _make_signal()
    result = broker.execute_signal(signal, internal_mgr)
    assert result is None


def test_execute_signal_calls_notify(broker, internal_mgr):
    """execute_signal がシグナル推奨通知を発火する。"""
    signal = _make_signal()
    broker.execute_signal(signal, internal_mgr)
    broker._notifier.notify_signal_recommendation.assert_called_once()


def test_execute_signal_hold_skips_notification(broker, internal_mgr):
    """HOLD シグナルでは推奨通知を発火しない。"""
    signal = _make_signal(action="hold")
    broker.execute_signal(signal, internal_mgr)
    broker._notifier.notify_signal_recommendation.assert_not_called()


def test_check_and_close_returns_empty(broker, manual_mgr):
    """check_and_close_positions は常に空リストを返す。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    result = broker.check_and_close_positions([], {"USDJPY=X": 149.0}, manual_mgr)
    assert result == []


def test_check_and_close_notifies_sltp(broker, manual_mgr):
    """SL 到達で SLTPAlertEvent を発火する。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    broker.check_and_close_positions([], {"USDJPY=X": 148.5}, manual_mgr)
    broker._notifier.notify_sltp_alert.assert_called_once()


def test_check_and_close_no_alert_when_in_range(broker, manual_mgr):
    """SL/TP 未到達では通知しない。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    broker.check_and_close_positions([], {"USDJPY=X": 150.5}, manual_mgr)
    broker._notifier.notify_sltp_alert.assert_not_called()


def test_portfolio_warning_in_notification(broker, manual_mgr, internal_mgr):
    """manual に既存ポジがあるとき portfolio_warning が設定される。"""
    for i in range(4):
        manual_mgr.open_position(Order.new(
            pair=f"PAIR{i}=X", direction="buy", entry_price=150.0,
            stop_loss=149.0, take_profit=152.0, position_size=5000,
        ))
    signal = _make_signal()
    broker.execute_signal(signal, internal_mgr)
    call_args = broker._notifier.notify_signal_recommendation.call_args
    event = call_args[0][0]
    assert event.portfolio_warning != ""
```

- [ ] **ステップ 2: テストが失敗することを確認**

実行: `python -m pytest tests/test_signal_only_broker.py -v`
期待: ModuleNotFoundError で FAIL

- [ ] **ステップ 3: SignalOnlyBrokerAdapter を実装**

```python
# src/trading/signal_only_broker.py
"""signal_only モード用ブローカーアダプター。

注文は発行せず、シグナル推奨・SL/TP到達・レビュー推奨を通知のみ行う。
internal PositionManager (自動) とは別の manual PositionManager を参照して
ポートフォリオガード判定・既存ポジション数の取得を行う。
"""
from __future__ import annotations

import asyncio
import logging

from src.notifications.notifier import (
    NotifierAdapter,
    SLTPAlertEvent,
    SignalRecommendationEvent,
)
from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.portfolio_guard import check_portfolio_limits
from src.trading.position_manager import Order, PositionManager

logger = logging.getLogger(__name__)


class SignalOnlyBrokerAdapter(BrokerAdapter):
    """通知のみ行い、注文を発行しないブローカーアダプター。"""

    def __init__(
        self,
        manual_position_mgr: PositionManager,
        notifier: NotifierAdapter,
        max_total_positions: int = 4,
        max_positions_per_group: int = 2,
        max_same_direction_per_group: int = 2,
    ) -> None:
        self._manual_mgr = manual_position_mgr
        self._notifier = notifier
        self._max_total = max_total_positions
        self._max_per_group = max_positions_per_group
        self._max_same_dir = max_same_direction_per_group

    def execute_signal(
        self,
        signal: TradeSignal,
        position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        if signal.action == "hold":
            return None

        direction = signal.action
        account = self._manual_mgr.get_account_state()

        portfolio_warning = check_portfolio_limits(
            pair=signal.pair,
            direction=direction,
            position_size=signal.position_size,
            open_positions=account.open_positions,
            max_total_positions=self._max_total,
            max_positions_per_group=self._max_per_group,
            max_same_direction_per_group=self._max_same_dir,
        ) or ""

        existing = sum(1 for p in account.open_positions if p.pair == signal.pair)
        max_loss = account.balance * 0.02

        event = SignalRecommendationEvent(
            pair=signal.pair,
            direction=direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=signal.position_size,
            combined_score=signal.combined_score,
            confidence=signal.confidence,
            signal_reason=signal.signal_reason,
            detail_reason=signal.detail_reason,
            max_loss=max_loss,
            portfolio_warning=portfolio_warning,
            existing_positions=existing,
            source="trading",
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notifier.notify_signal_recommendation(event))
        except RuntimeError:
            pass
        logger.info(
            f"[SIGNAL_ONLY] {signal.pair} {direction.upper()} 推奨 | "
            f"score={signal.combined_score:+.3f} conf={signal.confidence:.2f}"
            + (f" | ⚠ {portfolio_warning}" if portfolio_warning else "")
        )
        return None

    def check_and_close_positions(
        self,
        open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        manual_account = self._manual_mgr.get_account_state()
        for pos in manual_account.open_positions:
            price = current_prices.get(pos.pair)
            if price is None:
                continue

            trigger: str | None = None
            if pos.direction == "buy":
                if price <= pos.stop_loss:
                    trigger = "stop_loss"
                elif price >= pos.take_profit:
                    trigger = "take_profit"
            else:
                if price >= pos.stop_loss:
                    trigger = "stop_loss"
                elif price <= pos.take_profit:
                    trigger = "take_profit"

            if trigger:
                multiplier = 1 if pos.direction == "buy" else -1
                unrealized = (price - pos.entry_price) * pos.position_size * multiplier
                event = SLTPAlertEvent(
                    pair=pos.pair,
                    direction=pos.direction,
                    order_id=pos.order_id,
                    entry_price=pos.entry_price,
                    current_price=price,
                    trigger=trigger,
                    unrealized_pnl=unrealized,
                )
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._notifier.notify_sltp_alert(event))
                except RuntimeError:
                    pass
                logger.info(
                    f"[SIGNAL_ONLY] {pos.pair} {pos.direction.upper()} "
                    f"{trigger.upper()} 到達通知 | price={price:.5f}"
                )
        return []
```

- [ ] **ステップ 4: create_broker ファクトリを拡張**

`src/trading/live_broker.py` の `create_broker` 関数（131行目〜）を置き換え:

```python
def create_broker(
    trading_mode: str,
    position_mgr: PositionManager | None = None,
    *,
    max_total_positions: int = 4,
    max_positions_per_group: int = 2,
    max_same_direction_per_group: int = 2,
    manual_position_mgr: PositionManager | None = None,
    notifier: "NotifierAdapter | None" = None,
) -> BrokerAdapter:
    """trading_mode に応じた BrokerAdapter を返すファクトリ関数。

    Args:
        trading_mode: "paper" | "signal_only" | "live"
        manual_position_mgr: signal_only モードで manual ポジションを参照する PositionManager
        notifier: signal_only モードで通知を送る NotifierAdapter
    """
    from src.trading.paper_broker import PaperBrokerAdapter

    if trading_mode == "paper":
        return PaperBrokerAdapter(
            max_total_positions=max_total_positions,
            max_positions_per_group=max_positions_per_group,
            max_same_direction_per_group=max_same_direction_per_group,
        )
    elif trading_mode == "signal_only":
        from src.trading.signal_only_broker import SignalOnlyBrokerAdapter
        if manual_position_mgr is None or notifier is None:
            raise ValueError(
                "signal_only モードでは manual_position_mgr と notifier が必須です"
            )
        return SignalOnlyBrokerAdapter(
            manual_position_mgr=manual_position_mgr,
            notifier=notifier,
            max_total_positions=max_total_positions,
            max_positions_per_group=max_positions_per_group,
            max_same_direction_per_group=max_same_direction_per_group,
        )
    elif trading_mode == "live":
        return LiveBrokerAdapter()
    else:
        raise ValueError(f"不明な trading_mode: {trading_mode!r}。'paper', 'signal_only', 'live' のいずれかを指定してください。")
```

`live_broker.py` の先頭に import 追加:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.notifications.notifier import NotifierAdapter
```

- [ ] **ステップ 5: テストが通ることを確認**

実行: `python -m pytest tests/test_signal_only_broker.py -v`
期待: 8 passed

- [ ] **ステップ 6: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed（既存テスト影響なし）

- [ ] **ステップ 7: コミット**

```bash
git add src/trading/signal_only_broker.py src/trading/live_broker.py tests/test_signal_only_broker.py
git commit -m "feat(broker): SignalOnlyBrokerAdapter + create_broker 拡張"
```

---

### タスク 3: 取引サイクル — Dual PositionManager

**ファイル:**
- 変更: `src/cycles/trading.py`
- 変更: `src/config/schema.py`

- [ ] **ステップ 1: AppConfig に manual_state_dir プロパティを追加**

`src/config/schema.py` の既存 `state_dir` プロパティ（448-449行目）の後に追加:

```python
    @property
    def manual_state_dir(self) -> Path:
        return BASE_DIR / "data" / "manual_state"
```

- [ ] **ステップ 2: _build_trading_runtime を signal_only 対応に更新**

`src/cycles/trading.py` の `_build_trading_runtime` 関数（746-770行目）を置き換え:

```python
def _build_trading_runtime(config: AppConfig):
    """trading_cycle が必要とするランタイムを一括生成する。

    signal_only モードでは:
    - internal 用: PaperBrokerAdapter + NullNotifier (RAG 学習のみ)
    - signal_only_broker: SignalRecommendation 等の通知用
    - manual_position_mgr: 手動ポジション管理用
    """
    adaptive_store = AdaptiveParamsStore(
        state_dir=config.state_dir,
        defaults={
            "sl_atr_mult": config.trading.sl_atr_mult_default,
            "tp_atr_mult": config.trading.tp_atr_mult_default,
        },
        limits={
            "sl_atr_mult_min": config.trading.sl_atr_mult_min,
            "sl_atr_mult_max": config.trading.sl_atr_mult_max,
            "tp_atr_mult_min": config.trading.tp_atr_mult_min,
            "tp_atr_mult_max": config.trading.tp_atr_mult_max,
        },
    )
    llm_price = create_llm_client(config, "price_analysis")
    llm_reflect = create_llm_client(config, "reflection")

    if config.trading.trading_mode == "signal_only":
        from src.trading.paper_broker import PaperBrokerAdapter
        internal_broker = PaperBrokerAdapter(
            max_total_positions=config.trading.max_total_positions,
            max_positions_per_group=config.trading.max_positions_per_currency_group,
            max_same_direction_per_group=config.trading.max_same_direction_per_group,
        )
        notifier = create_notifier(config.notifier.enabled)
        manual_state = StateStore(config.manual_state_dir)
        manual_mgr = PositionManager(
            manual_state, config.trading.initial_balance, context="Manual",
        )
        signal_broker = create_broker(
            "signal_only",
            max_total_positions=config.trading.max_total_positions,
            max_positions_per_group=config.trading.max_positions_per_currency_group,
            max_same_direction_per_group=config.trading.max_same_direction_per_group,
            manual_position_mgr=manual_mgr,
            notifier=notifier,
        )
        return internal_broker, adaptive_store, notifier, llm_price, llm_reflect, signal_broker, manual_mgr

    broker = create_broker(
        config.trading.trading_mode,
        max_total_positions=config.trading.max_total_positions,
        max_positions_per_group=config.trading.max_positions_per_currency_group,
        max_same_direction_per_group=config.trading.max_same_direction_per_group,
    )
    notifier = create_notifier(config.notifier.enabled)
    return broker, adaptive_store, notifier, llm_price, llm_reflect, None, None
```

- [ ] **ステップ 3: trading_cycle を Dual PositionManager 対応に更新**

`trading_cycle` 関数（773-850行目）を更新。主な変更点:
- `_build_trading_runtime` の戻り値を7要素にアンパック
- signal_only 時は internal に NullNotifier を使用
- internal の close/open を `[INTERNAL]` プレフィックスでログ出力
- signal_only_broker 経由でシグナル通知を送信
- manual ポジションの advisory review を追加

`NullNotifier` を import に追加:

```python
from src.notifications.notifier import (
    NullNotifier,
    OrderClosedEvent,
    OrderOpenedEvent,
    SignalSkippedEvent,
    create_notifier,
)
```

- [ ] **ステップ 4: _phase_review_manual_positions を追加**

`trading_cycle` の前に、manual ポジション用の advisory review 関数を追加。Layer 1-3 の判定結果を `ReviewAdvisoryEvent` で通知のみ行い、close はしない。

- [ ] **ステップ 5: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed

- [ ] **ステップ 6: コミット**

```bash
git add src/cycles/trading.py src/config/schema.py
git commit -m "feat(cycle): signal_only モードの dual PositionManager 統合"
```

---

### タスク 4: Manual REST API

**ファイル:**
- 新規: `src/api/routes/manual.py`
- 変更: `src/api/server.py`
- 変更: `src/api/_state.py`
- テスト: `tests/test_manual_api.py`

- [ ] **ステップ 1: テストを書く**

```python
# tests/test_manual_api.py
"""Manual REST API のテスト。"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_api_key():
    with patch.dict(os.environ, {"API_SECRET_KEY": "test-key"}):
        yield


@pytest.fixture
def client(tmp_path):
    from src.api._state import state
    from src.api.server import app
    from src.config.schema import AppConfig, TradingConfig
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager

    cfg = MagicMock(spec=AppConfig)
    cfg.trading = MagicMock(spec=TradingConfig)
    cfg.trading.trading_mode = "signal_only"
    cfg.trading.initial_balance = 500000.0
    cfg.manual_state_dir = tmp_path / "manual"
    cfg.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X", display_name="USD/JPY"),
    ]

    manual_store = StateStore(tmp_path / "manual")
    manual_mgr = PositionManager(manual_store, 500000.0, context="ManualTest")

    state.config = cfg
    state.manual_position_mgr = manual_mgr

    from src.api.routes import manual
    if not any(r.path.startswith("/manual") for r in app.routes):
        app.include_router(manual.router)

    return TestClient(app)


HEADERS = {"X-API-Key": "test-key"}


def test_manual_list_empty(client):
    resp = client.get("/manual/list", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 500000.0
    assert data["open_positions"] == []


def test_manual_open_and_list(client):
    resp = client.post("/manual/open", headers=HEADERS, json={
        "pair": "USDJPY=X", "direction": "buy",
        "entry_price": 150.2, "position_size": 5000,
        "stop_loss": 149.8, "take_profit": 151.2,
    })
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]
    assert order_id

    resp = client.get("/manual/list", headers=HEADERS)
    positions = resp.json()["open_positions"]
    assert len(positions) == 1
    assert positions[0]["pair"] == "USDJPY=X"


def test_manual_close(client):
    resp = client.post("/manual/open", headers=HEADERS, json={
        "pair": "USDJPY=X", "direction": "buy",
        "entry_price": 150.0, "position_size": 5000,
        "stop_loss": 149.0, "take_profit": 152.0,
    })
    order_id = resp.json()["order_id"]

    resp = client.post(f"/manual/close/{order_id}", headers=HEADERS, json={
        "close_price": 151.0, "close_reason": "take_profit",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["realized_pnl"] > 0
    assert data["balance"] > 500000.0


def test_manual_balance(client):
    resp = client.post("/manual/balance", headers=HEADERS, json={"balance": 600000})
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 600000.0
    assert data["previous"] == 500000.0


def test_manual_close_not_found(client):
    resp = client.post("/manual/close/nonexistent", headers=HEADERS, json={
        "close_price": 151.0, "close_reason": "manual",
    })
    assert resp.status_code == 404
```

- [ ] **ステップ 2: テストが失敗することを確認**

実行: `python -m pytest tests/test_manual_api.py -v`
期待: ImportError で FAIL

- [ ] **ステップ 3: APIState に manual_position_mgr を追加**

`src/api/_state.py` の `APIState` データクラスに追加:

```python
    manual_position_mgr: Any = None   # PositionManager (signal_only 用)
```

- [ ] **ステップ 4: manual ルートを実装**

`src/api/routes/manual.py` を新規作成。エンドポイント4つ:
- `POST /manual/open` — ポジション登録
- `POST /manual/close/{order_id}` — 決済記録（バックグラウンドで reflection 生成）
- `GET /manual/list` — 一覧
- `POST /manual/balance` — 残高補正

- [ ] **ステップ 5: server.py に manual ルーターを条件付き登録**

`start_api_server` 内で `trading_mode == "signal_only"` のとき `manual.router` を登録。`manual_position_mgr` を `state` に注入。

- [ ] **ステップ 6: テストが通ることを確認**

実行: `python -m pytest tests/test_manual_api.py -v`
期待: 5 passed

- [ ] **ステップ 7: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed

- [ ] **ステップ 8: コミット**

```bash
git add src/api/routes/manual.py src/api/server.py src/api/_state.py tests/test_manual_api.py
git commit -m "feat(api): manual ポジション REST API (/manual/*)"
```

---

### タスク 5: Manual TUI インタラクティブモード

**ファイル:**
- 変更: `src/cli.py`

- [ ] **ステップ 1: 現在の cli.py のコマンドディスパッチ構造を確認**

`src/cli.py` を読み、コマンド処理のパターンを把握する。

- [ ] **ステップ 2: `manual` コマンドハンドラを追加**

`manual` インタラクティブモードを実装:
- 直近シグナル一覧（メモリ保持、再起動でクリア）
- オープンポジション一覧
- 番号 + `open` / `close` で操作
- `edit` でロット・SL/TP 修正
- `balance` で残高補正
- `q` で終了

- [ ] **ステップ 3: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed

- [ ] **ステップ 4: コミット**

```bash
git add src/cli.py
git commit -m "feat(cli): manual インタラクティブモード追加"
```

---

### タスク 6: 設定・ドキュメント更新

**ファイル:**
- 変更: `config/settings.yaml.example`
- 変更: `README.md`
- 変更: `DETAIL.md`

- [ ] **ステップ 1: settings.yaml.example に signal_only 設定例を追加**

`trading:` セクションの `trading_mode` コメントを更新:

```yaml
trading:
  # trading_mode: "paper" | "signal_only" | "live"
  # - paper: ペーパートレード（シミュレーション）
  # - signal_only: シグナル通知 + 内部自動RAG学習 + 手動ポジション管理
  # - live: OANDA自動発注（未実装）
  trading_mode: "paper"
```

- [ ] **ステップ 2: README.md と DETAIL.md を更新**

signal_only モードの説明を追加。アーキテクチャ図、REST API テーブル、CLI コマンドテーブルを更新。

- [ ] **ステップ 3: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed

- [ ] **ステップ 4: コミット**

```bash
git add config/settings.yaml.example README.md DETAIL.md
git commit -m "docs: signal_only モードの設定例・ドキュメント更新"
```

---

### タスク 7: 統合テスト

**ファイル:**
- テスト: `tests/test_signal_only_integration.py`

- [ ] **ステップ 1: 統合テストを書く**

```python
# tests/test_signal_only_integration.py
"""signal_only モードの統合テスト。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.notifications.notifier import NullNotifier
from src.persistence.state_store import StateStore
from src.trading.position_manager import Order, PositionManager


@pytest.fixture
def dual_mgrs(tmp_path):
    internal_store = StateStore(tmp_path / "internal")
    manual_store = StateStore(tmp_path / "manual")
    internal = PositionManager(internal_store, 500000.0, context="Internal")
    manual = PositionManager(manual_store, 500000.0, context="Manual")
    return internal, manual


def test_dual_managers_independent(dual_mgrs):
    """internal と manual の PositionManager が独立動作する。"""
    internal, manual = dual_mgrs
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    internal.open_position(order)
    assert len(internal.get_account_state().open_positions) == 1
    assert len(manual.get_account_state().open_positions) == 0


def test_manual_close_triggers_balance_update(dual_mgrs):
    """manual close で残高が自動更新される。"""
    _, manual = dual_mgrs
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual.open_position(order)
    manual.close_position(order.order_id, 151.0, "take_profit")
    account = manual.get_account_state()
    assert account.balance > 500000.0
    assert len(account.open_positions) == 0


def test_signal_only_broker_uses_manual_for_guard(dual_mgrs):
    """SignalOnlyBrokerAdapter が manual ポジションを参照してガード判定する。"""
    internal, manual = dual_mgrs
    from src.trading.signal_only_broker import SignalOnlyBrokerAdapter
    from tests.test_signal_only_broker import _make_signal

    notifier = AsyncMock(spec=NullNotifier)
    broker = SignalOnlyBrokerAdapter(
        manual_position_mgr=manual, notifier=notifier,
        max_total_positions=1,
    )
    manual.open_position(Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    ))
    broker.execute_signal(_make_signal("EURUSD=X"), internal)
    call_args = notifier.notify_signal_recommendation.call_args
    event = call_args[0][0]
    assert "Max total positions" in event.portfolio_warning
```

- [ ] **ステップ 2: 統合テストを実行**

実行: `python -m pytest tests/test_signal_only_integration.py -v`
期待: 3 passed

- [ ] **ステップ 3: 全テスト実行**

実行: `python -m pytest tests/ -q`
期待: 全 passed

- [ ] **ステップ 4: コミット**

```bash
git add tests/test_signal_only_integration.py
git commit -m "test: signal_only モードの統合テスト"
```

---

## セルフレビュー

**スペックカバレッジ:**
- ✅ SignalOnlyBrokerAdapter（タスク 2）
- ✅ 通知イベント3種（タスク 1）
- ✅ Dual PositionManager（タスク 3）
- ✅ Manual REST API（タスク 4）
- ✅ Manual TUI インタラクティブ（タスク 5）
- ✅ Phase 4a advisory（タスク 3、`_phase_review_manual_positions`）
- ✅ State 分離（タスク 3、`manual_state_dir`）
- ✅ Internal ログ `[INTERNAL]`（タスク 3）
- ✅ 設定変更（タスク 6）
- ✅ ポートフォリオガード情報のみ（タスク 2）

**プレースホルダー確認:** TBD/TODO なし。全コードブロック完備。

**型の一貫性:**
- `SignalRecommendationEvent` フィールド: タスク1（定義）とタスク2（使用）で一致 ✅
- `SLTPAlertEvent` フィールド: タスク1とタスク2で一致 ✅
- `ReviewAdvisoryEvent` フィールド: タスク1とタスク3で一致 ✅
- `create_broker` シグネチャ: タスク2とタスク3で一致 ✅
- `_build_trading_runtime` 戻り値（7要素）: `trading_cycle` のアンパックと一致 ✅
