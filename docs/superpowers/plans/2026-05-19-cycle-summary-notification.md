# 取引サイクル集約通知 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取引サイクルの Discord 通知を「シグナル1件ごとに1通」から「1サイクル1通の集約サマリー」へ変更する。

**Architecture:** Phase 4b の各シグナル処理で通知を即発火せず `SignalOutcome` を蓄積し、Phase 4b 後に `CycleSummaryEvent` を作って `notify_cycle_summary()` を1回呼ぶ。notifier はステートレスのまま。決済通知は従来どおり個別即時。`notify_on_cycle_summary=False` で旧 per-event 通知へ切り戻せる。

**Tech Stack:** Python 3 / dataclasses / pytest (`@pytest.mark.asyncio`) / Discord webhook。

**設計書:** `docs/superpowers/specs/2026-05-19-cycle-summary-notification-design.md`

**作業ブランチ:** `feat/cycle-summary-notification` (既存)。全タスクをこのブランチへコミットする。

---

## File Structure

| ファイル | 役割 | 変更種別 |
|---|---|---|
| `src/signals/signal_combiner.py` | `TradeSignal` に `tv_recommendation` フィールド追加 | Modify |
| `src/notifications/notifier.py` | `SignalOutcome` / `CycleSummaryEvent` / `_format_signal_block` / `_format_cycle_summary` / `notify_cycle_summary` | Modify |
| `src/config/schema.py` | `NotifierConfig.notify_on_cycle_summary` | Modify |
| `config/settings.yaml` / `config/settings.yaml.example` | `notify_on_cycle_summary` キー | Modify |
| `src/cycles/trading.py` | `PairAnalysisOutcome` / `PairAnalysisError` / 各 Phase 関数の戻り値変更 / 集約通知呼び出し | Modify |
| `tests/test_signal_combiner.py` | `tv_recommendation` の回帰テストを追記 (既存 `combine_signals` テストファイル) | Modify |
| `tests/test_cycle_summary.py` | dataclass / config / 整形関数 / `notify_cycle_summary` のテスト | Create |
| `tests/test_trading_cycle_summary.py` | 分析 Phase / 実行 Phase / halt サマリーの配線テスト | Create |
| `tests/test_trading_cycle_helpers.py` | `_adjust_signal_with_rag` 戻り値テストを追加 | Modify |
| `tests/test_trading_cycle_halt.py` | 既存モックを新シグネチャへ追従 | Modify |

タスク順序は依存関係順。各タスク完了時点で全テストが green になるよう設計してある。

---

### Task 1: `TradeSignal.tv_recommendation` フィールド追加

**Files:**
- Modify: `src/signals/signal_combiner.py` (`TradeSignal` dataclass, 21-35行付近)
- Modify: `tests/test_signal_combiner.py` (既存ファイル — `combine_signals` のテストが既にある。末尾へ追記し、既存テストは保持する)

> 実装時の訂正: 当初この計画は `tests/test_signal_combiner.py` を新規ファイルと誤記していた。実際は既存ファイル。下記テストは**末尾へ追記**し、`import` に `from datetime import datetime` と `TradeSignal` を加える。既存の `combine_signals` テストを削除しないこと。

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_signal_combiner.py`:

```python
"""signal_combiner.TradeSignal に関するテスト。"""
from __future__ import annotations

from datetime import datetime

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.signals.signal_combiner import TradeSignal


def _bare_signal() -> TradeSignal:
    return TradeSignal(
        pair="USDJPY=X", action="buy", predicted_direction="bullish",
        combined_score=0.3, confidence=0.7,
        entry_price=159.0, stop_loss=158.0, take_profit=161.0, position_size=1000.0,
        signal_reason="test", detail_reason="",
        news=NewsSentiment(pair="USDJPY=X", sentiment_score=0.1, confidence=0.5),
        price=PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.3, confidence=0.7,
            entry_zone=(158.0, 160.0), reasoning_summary="t",
            analyzed_at=datetime(2026, 5, 19, 12, 0),
        ),
        generated_at=datetime(2026, 5, 19, 12, 0),
    )


def test_trade_signal_tv_recommendation_defaults_empty():
    """tv_recommendation を渡さなければ空文字。"""
    assert _bare_signal().tv_recommendation == ""
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_signal_combiner.py -v`
Expected: FAIL — `AttributeError: 'TradeSignal' object has no attribute 'tv_recommendation'`

- [ ] **Step 3: フィールドを追加**

`src/signals/signal_combiner.py` の `TradeSignal` dataclass、`generated_at` の直後に1行追加:

```python
    news: NewsSentiment
    price: PriceAnalysis
    generated_at: datetime
    tv_recommendation: str = ""  # TradingView コンセンサス推奨 (例 "BUY"/"STRONG_SELL")。未取得時 ""
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_signal_combiner.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/signals/signal_combiner.py tests/test_signal_combiner.py
git commit -m "feat: TradeSignal に tv_recommendation フィールドを追加"
```

---

### Task 2: データ構造と config フラグの追加

`SignalOutcome` / `CycleSummaryEvent` (notifier.py) と `NotifierConfig.notify_on_cycle_summary` (schema.py + yaml) を追加する。

**Files:**
- Modify: `src/notifications/notifier.py` (import 行 + `PriceAlertEvent` の直後)
- Modify: `src/config/schema.py` (`NotifierConfig`, 327-332行付近)
- Modify: `config/settings.yaml` (297行付近), `config/settings.yaml.example` (297行付近)
- Test: `tests/test_cycle_summary.py` (新規)

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_cycle_summary.py`:

```python
"""取引サイクル集約通知 (notifier.py) のテスト。"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.config.schema import NotifierConfig
from src.notifications.notifier import CycleSummaryEvent, SignalOutcome


def test_signal_outcome_defaults():
    o = SignalOutcome(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.75, combined_score=0.32,
        reason="r", detail_reason="d",
        news_score=0.12, tech_score=0.37,
    )
    assert o.tv_recommendation == ""
    assert o.rag_note == ""
    assert o.order is None


def test_cycle_summary_event_defaults():
    ev = CycleSummaryEvent(cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[])
    assert ev.halted is False
    assert ev.data_health == []
    assert ev.source == "trading"


def test_notifier_config_has_cycle_summary_flag():
    assert NotifierConfig().notify_on_cycle_summary is True
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_cycle_summary.py -v`
Expected: FAIL — `ImportError: cannot import name 'SignalOutcome'`

- [ ] **Step 3a: notifier.py の import を更新**

`src/notifications/notifier.py` 先頭の import を変更する。現状:

```python
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
```

を次へ:

```python
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from src.trading.position_manager import Order
```

- [ ] **Step 3b: SignalOutcome と CycleSummaryEvent を追加**

`src/notifications/notifier.py` の `PriceAlertEvent` dataclass の直後 (`# ── 抽象基底クラス` コメントの前) に追加:

```python
@dataclass
class SignalOutcome:
    """1シグナルの発注判断結果。集約サマリー整形専用の純粋なデータ構造。"""
    pair: str
    action: str                  # "buy" | "sell" | "hold"
    status: str                  # executed/hold/skipped/halted/rejected/failed
    confidence: float
    combined_score: float
    reason: str                  # executed/hold→signal_reason / それ以外→ExecutionResult.reason
    detail_reason: str           # ニュース/テクニカル詳細内訳
    news_score: float            # signal.news.sentiment_score — drivers 行
    tech_score: float            # signal.price.bias_score — drivers 行
    tv_recommendation: str = ""  # signal.tv_recommendation — drivers 行 ("" なら非表示)
    rag_note: str = ""           # RAG 補正が action/score を変えたときの注記 ("" なら非表示)
    order: Order | None = None   # status=="executed" のとき約定 Order


@dataclass
class CycleSummaryEvent:
    """notify_cycle_summary に渡す、1取引サイクルの集約結果。"""
    cycle_time: datetime
    outcomes: list[SignalOutcome]
    halted: bool = False
    data_health: list[str] = field(default_factory=list)  # 問題文字列。空なら Data 行なし
    source: str = "trading"
```

- [ ] **Step 3c: NotifierConfig にフラグを追加**

`src/config/schema.py` の `NotifierConfig`。現状:

```python
@dataclass
class NotifierConfig:
    enabled: bool = False                 # true で Discord 通知を有効化
    notify_on_order_open: bool = True
    notify_on_order_close: bool = True
    notify_on_signal_skipped: bool = True
    notify_on_price_alert: bool = True    # 価格急変動通知
```

を次へ:

```python
@dataclass
class NotifierConfig:
    enabled: bool = False                 # true で Discord 通知を有効化
    notify_on_order_open: bool = True
    notify_on_order_close: bool = True
    notify_on_signal_skipped: bool = True
    notify_on_price_alert: bool = True    # 価格急変動通知
    notify_on_cycle_summary: bool = True  # 取引サイクル結果を1通に集約 (false で旧 per-event 通知)
```

- [ ] **Step 3d: config yaml にキーを追加**

`config/settings.yaml.example` — `notify_on_price_alert: true      # 価格急変動（損失方向）を検知した場合` の行の直後に追加:

```yaml
  notify_on_cycle_summary: true    # 取引サイクル結果を1通に集約して通知 (false で旧 per-event 通知)
```

`config/settings.yaml` — `notify_on_price_alert: true    # 価格急変動（損失方向）を検知した場合` の行の直後に追加:

```yaml
  notify_on_cycle_summary: true  # 取引サイクル結果を1通に集約して通知 (false で旧 per-event 通知)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_cycle_summary.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: コミット**

```bash
git add src/notifications/notifier.py src/config/schema.py config/settings.yaml config/settings.yaml.example tests/test_cycle_summary.py
git commit -m "feat: SignalOutcome/CycleSummaryEvent と notify_on_cycle_summary を追加"
```

---

### Task 3: `_format_signal_block` — 1シグナルブロックの整形

**Files:**
- Modify: `src/notifications/notifier.py` (`class NotifierAdapter` の直前)
- Test: `tests/test_cycle_summary.py` (追記)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_cycle_summary.py` の末尾に追記:

```python
from src.notifications.notifier import _format_signal_block  # noqa: E402
from src.trading.position_manager import Order  # noqa: E402


def _executed_outcome(**kw) -> SignalOutcome:
    order = Order.new("USDJPY=X", "buy", 159.004, 158.216, 160.580, 1000.0)
    defaults = dict(
        pair="USDJPY=X", action="buy", status="executed",
        confidence=0.75, combined_score=0.320,
        reason="rates higher + tech long alignment", detail_reason="",
        news_score=0.12, tech_score=0.37, tv_recommendation="BUY", order=order,
    )
    defaults.update(kw)
    return SignalOutcome(**defaults)


def _hold_outcome(**kw) -> SignalOutcome:
    defaults = dict(
        pair="EURUSD=X", action="hold", status="hold",
        confidence=0.30, combined_score=-0.023,
        reason="confidence too low, NEWS/PRICE conflict", detail_reason="",
        news_score=0.09, tech_score=-0.05, tv_recommendation="STRONG_SELL",
    )
    defaults.update(kw)
    return SignalOutcome(**defaults)


def test_format_signal_block_executed_has_all_lines():
    block = _format_signal_block(_executed_outcome())
    assert "📈 USDJPY=X BUY EXECUTED" in block
    assert "score +0.320 | conf 75% | RR 2.00" in block
    assert "entry 159.00400" in block
    assert "SL 158.21600" in block
    assert "drivers: News +0.12 / Tech +0.37 / TV BUY" in block
    assert "reason: rates higher" in block


def test_format_signal_block_hold_omits_entry_sl_tp_and_rr():
    block = _format_signal_block(_hold_outcome())
    assert "⏸ EURUSD=X HOLD" in block
    assert "score -0.023 | conf 30%" in block
    assert "entry" not in block
    assert "RR" not in block
    assert "drivers: News +0.09 / Tech -0.05 / TV STRONG_SELL" in block
    assert "reason: confidence too low" in block


def test_format_signal_block_rejected_shows_reason_not_existing_position():
    o = _hold_outcome(
        pair="EURUSD=X", action="sell", status="rejected",
        reason="発注拒否 (broker): retcode=10016 Invalid stops",
    )
    block = _format_signal_block(o)
    assert "🚫 EURUSD=X SELL REJECTED" in block
    assert "retcode=10016" in block
    assert "既存ポジション" not in block


def test_format_signal_block_failed():
    block = _format_signal_block(
        _hold_outcome(pair="USDJPY=X", action="buy", status="failed",
                      reason="bridge unreachable"))
    assert "❌ USDJPY=X BUY FAILED" in block
    assert "reason: bridge unreachable" in block


def test_format_signal_block_skipped_atr_reason():
    block = _format_signal_block(
        _hold_outcome(pair="USDJPY=X", action="buy", status="skipped",
                      reason="ATR SL/TP calculation failed"))
    assert "⏭ USDJPY=X BUY SKIPPED" in block
    assert "reason: ATR SL/TP calculation failed" in block


def test_format_signal_block_omits_tv_when_empty():
    block = _format_signal_block(_hold_outcome(tv_recommendation=""))
    assert "TV" not in block
    assert "drivers: News +0.09 / Tech -0.05" in block


def test_format_signal_block_shows_rag_note():
    block = _format_signal_block(
        _hold_outcome(rag_note="score -0.023→+0.115, hold→buy"))
    assert "RAG: score -0.023→+0.115, hold→buy" in block


def test_format_signal_block_no_rag_line_when_empty():
    assert "RAG:" not in _format_signal_block(_hold_outcome())


def test_format_signal_block_scale_in_label():
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0, is_scale_in=True)
    block = _format_signal_block(_executed_outcome(order=order))
    assert "(scale-in)" in block
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_cycle_summary.py -k format_signal_block -v`
Expected: FAIL — `ImportError: cannot import name '_format_signal_block'`

- [ ] **Step 3: `_format_signal_block` を実装**

`src/notifications/notifier.py` の `class NotifierAdapter(ABC):` の直前に追加:

```python
def _format_signal_block(o: SignalOutcome) -> str:
    """1シグナルの結果ブロックを整形する。"""
    if o.status == "executed":
        emoji = "📈" if o.action == "buy" else "📉"
        label = f"{o.action.upper()} EXECUTED"
        if o.order is not None and getattr(o.order, "is_scale_in", False):
            label += " (scale-in)"
    elif o.status == "hold":
        emoji, label = "⏸", "HOLD"
    elif o.status == "rejected":
        emoji, label = "🚫", f"{o.action.upper()} REJECTED"
    elif o.status == "failed":
        emoji, label = "❌", f"{o.action.upper()} FAILED"
    else:  # skipped / halted
        emoji = "⏭"
        label = f"{o.action.upper()} SKIPPED" if o.action in ("buy", "sell") else "SKIPPED"

    lines = [f"{emoji} {o.pair} {label}"]

    score_line = f"score {o.combined_score:+.3f} | conf {o.confidence:.0%}"
    if o.status == "executed" and o.order is not None:
        entry, sl, tp = o.order.entry_price, o.order.stop_loss, o.order.take_profit
        sl_dist = abs(entry - sl)
        rr = abs(tp - entry) / sl_dist if sl_dist > 0 else 0.0
        score_line += f" | RR {rr:.2f}"
    lines.append(score_line)

    if o.status == "executed" and o.order is not None:
        lines.append(
            f"entry {o.order.entry_price:.5f} | "
            f"SL {o.order.stop_loss:.5f} | TP {o.order.take_profit:.5f}"
        )

    drivers = f"drivers: News {o.news_score:+.2f} / Tech {o.tech_score:+.2f}"
    if o.tv_recommendation:
        drivers += f" / TV {o.tv_recommendation}"
    lines.append(drivers)

    if o.reason:
        lines.append(f"reason: {o.reason}")
    if o.rag_note:
        lines.append(f"RAG: {o.rag_note}")

    return "\n".join(lines)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_cycle_summary.py -k format_signal_block -v`
Expected: PASS (9 tests)

- [ ] **Step 5: コミット**

```bash
git add src/notifications/notifier.py tests/test_cycle_summary.py
git commit -m "feat: _format_signal_block でシグナル結果ブロックを整形"
```

---

### Task 4: `_format_cycle_summary` と `notify_cycle_summary`

**Files:**
- Modify: `src/notifications/notifier.py` (`_format_signal_block` の直後 + `NotifierAdapter` 内)
- Test: `tests/test_cycle_summary.py` (追記)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_cycle_summary.py` の末尾に追記:

```python
from src.notifications.notifier import (  # noqa: E402
    NotifierAdapter,
    _format_cycle_summary,
)


class _CapturingNotifier(NotifierAdapter):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def test_format_cycle_summary_header_and_counts():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30),
        outcomes=[_executed_outcome(), _hold_outcome()],
    )
    msg = _format_cycle_summary(event)
    assert msg.startswith("🟢 取引サイクル 17:30 JST")
    assert "結果: 1発注 / 1HOLD / 0拒否 / 0失敗" in msg
    assert "📈 USDJPY=X BUY EXECUTED" in msg
    assert "⏸ EURUSD=X HOLD" in msg


def test_format_cycle_summary_warning_emoji_on_rejection():
    o_rej = _hold_outcome(action="sell", status="rejected", reason="retcode=10016")
    event = CycleSummaryEvent(cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[o_rej])
    msg = _format_cycle_summary(event)
    assert msg.startswith("⚠️")
    assert "0発注 / 0HOLD / 1拒否 / 0失敗" in msg


def test_format_cycle_summary_skip_count_only_when_positive():
    no_skip = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    assert "スキップ" not in _format_cycle_summary(no_skip)
    o_skip = _hold_outcome(action="buy", status="skipped", reason="既存ポジションあり")
    with_skip = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[o_skip])
    assert "1スキップ" in _format_cycle_summary(with_skip)


def test_format_cycle_summary_data_health_line():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()],
        data_health=["EURUSD=X 分析失敗"],
    )
    msg = _format_cycle_summary(event)
    assert "⚠ Data: EURUSD=X 分析失敗" in msg
    assert msg.startswith("⚠️")


def test_format_cycle_summary_no_data_line_when_healthy():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    assert "Data:" not in _format_cycle_summary(event)


def test_format_cycle_summary_halt():
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[], halted=True)
    msg = _format_cycle_summary(event)
    assert msg.startswith("🛑 取引サイクル 17:30 JST")
    assert "halt 中" in msg
    assert "新規発注分析をスキップ" in msg


@pytest.mark.asyncio
async def test_notify_cycle_summary_calls_send():
    notifier = _CapturingNotifier()
    event = CycleSummaryEvent(
        cycle_time=datetime(2026, 5, 19, 17, 30), outcomes=[_hold_outcome()])
    await notifier.notify_cycle_summary(event)
    assert len(notifier.messages) == 1
    assert "取引サイクル 17:30 JST" in notifier.messages[0]
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_cycle_summary.py -k cycle_summary -v`
Expected: FAIL — `ImportError: cannot import name '_format_cycle_summary'`

- [ ] **Step 3a: `_format_cycle_summary` を実装**

`src/notifications/notifier.py` の `class NotifierAdapter(ABC):` の直前 (= `_format_signal_block` の直後) に追加:

```python
def _format_cycle_summary(event: CycleSummaryEvent) -> str:
    """1取引サイクルの集約サマリーのメッセージ文字列を組み立てる。"""
    hhmm = event.cycle_time.strftime("%H:%M")

    if event.halted:
        return (
            f"🛑 取引サイクル {hhmm} JST\n"
            "halt 中 — 新規発注分析をスキップ\n"
            "既存ポジション管理 (timeout 判定) のみ継続"
        )

    n_exec = sum(1 for o in event.outcomes if o.status == "executed")
    n_hold = sum(1 for o in event.outcomes if o.status == "hold")
    n_rej = sum(1 for o in event.outcomes if o.status == "rejected")
    n_fail = sum(1 for o in event.outcomes if o.status == "failed")
    n_skip = sum(1 for o in event.outcomes if o.status in ("skipped", "halted"))

    has_problem = n_rej > 0 or n_fail > 0 or bool(event.data_health)
    header_emoji = "⚠️" if has_problem else "🟢"

    counts = f"{n_exec}発注 / {n_hold}HOLD / {n_rej}拒否 / {n_fail}失敗"
    if n_skip > 0:
        counts += f" / {n_skip}スキップ"

    lines = [f"{header_emoji} 取引サイクル {hhmm} JST", f"結果: {counts}"]
    if event.data_health:
        lines.append("⚠ Data: " + " / ".join(event.data_health))
    for o in event.outcomes:
        lines.append("")
        lines.append(_format_signal_block(o))

    msg = "\n".join(lines)
    if len(msg) > 1900:  # Discord content 上限 2000 字に対する安全マージン
        msg = msg[:1900] + "\n…(以下省略)"
    return msg
```

- [ ] **Step 3b: `notify_cycle_summary` メソッドを追加**

`src/notifications/notifier.py` の `NotifierAdapter` クラス内、`notify_signal_skipped` メソッド (末尾は `await self.send(msg)`) の直後に追加:

```python
    async def notify_cycle_summary(self, event: CycleSummaryEvent) -> None:
        """取引サイクルの集約サマリーを送信する。"""
        await self.send(_format_cycle_summary(event))
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_cycle_summary.py -v`
Expected: PASS (全 20 tests)

- [ ] **Step 5: コミット**

```bash
git add src/notifications/notifier.py tests/test_cycle_summary.py
git commit -m "feat: notify_cycle_summary でサイクル集約サマリーを送信"
```

---

### Task 5: 分析 Phase — `PairAnalysisOutcome` / `PairAnalysisError`

`_process_pair` の戻り値を dataclass 化し、`_phase_analyze_pairs` が失敗ペアを特定して `data_health` を返すようにする。

**Files:**
- Modify: `src/cycles/trading.py` (import + `_process_pair` + `bounded` + `_phase_analyze_pairs` + `trading_cycle` Phase 3)
- Test: `tests/test_trading_cycle_summary.py` (新規)
- Modify: `tests/test_trading_cycle_halt.py` (`_phase_analyze_pairs` モック)

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_trading_cycle_summary.py`:

```python
"""取引サイクル集約通知の配線テスト (分析 Phase / 実行 Phase / halt サマリー)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_pair_analysis_outcome_and_error_construct():
    from src.cycles.trading import PairAnalysisError, PairAnalysisOutcome

    out = PairAnalysisOutcome(signal=MagicMock(), macro_ctx="m")
    assert out.tech_fallback is False
    err = PairAnalysisError(pair="USDJPY=X", error=RuntimeError("x"))
    assert err.pair == "USDJPY=X"


@pytest.mark.asyncio
async def test_phase_analyze_pairs_collects_data_health(monkeypatch):
    from src.cycles.trading import (
        PairAnalysisError,
        PairAnalysisOutcome,
        _phase_analyze_pairs,
    )

    sig_ok = MagicMock(pair="USDJPY=X")

    async def fake_process(pair_cfg, *a, **k):
        if pair_cfg.symbol == "USDJPY=X":
            return PairAnalysisOutcome(signal=sig_ok, macro_ctx="m", tech_fallback=True)
        return PairAnalysisError(pair="EURUSD=X", error=RuntimeError("boom"))

    monkeypatch.setattr("src.cycles.trading._process_pair", fake_process)

    config = MagicMock()
    config.llm.provider_config.max_concurrent = 2
    config.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X"), MagicMock(symbol="EURUSD=X"),
    ]
    signals, macro_ctxs, data_health = await _phase_analyze_pairs(
        config, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), None,
    )
    assert signals == [sig_ok]
    assert macro_ctxs == {"USDJPY=X": "m"}
    assert any("EURUSD=X 分析失敗" in d for d in data_health)
    assert any("USDJPY=X" in d and "fallback" in d for d in data_health)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_trading_cycle_summary.py -v`
Expected: FAIL — `ImportError: cannot import name 'PairAnalysisOutcome'`

- [ ] **Step 3a: trading.py の import を更新**

`src/cycles/trading.py` の先頭付近。`import logging` の直後に `from dataclasses import dataclass` を追加する:

```python
import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
```

`TYPE_CHECKING` ブロック (現状 `from src.trading.bridge_health_gate import BridgeHealthGate` のみ) に1行追加:

```python
if TYPE_CHECKING:
    from src.signals.signal_combiner import TradeSignal
    from src.trading.bridge_health_gate import BridgeHealthGate
```

- [ ] **Step 3b: dataclass を追加**

`src/cycles/trading.py` の `logger = logging.getLogger(__name__)` の直後、`_process_pair` 定義の前に追加:

```python
@dataclass
class PairAnalysisOutcome:
    """_process_pair の成功戻り値。"""
    signal: "TradeSignal"
    macro_ctx: str
    tech_fallback: bool = False  # 蓄積スナップショットがなく即時 Ollama 分析へ fallback した


@dataclass
class PairAnalysisError:
    """_process_pair の失敗戻り値。失敗ペアの symbol を保持する。"""
    pair: str
    error: Exception
```

- [ ] **Step 3c: `_process_pair` を更新**

3箇所を変更する。

(1) `price = analysis_store.aggregate(...)` の直後に `tech_fallback` を算出。現状:

```python
        price = analysis_store.aggregate(
            pair_cfg.symbol,
            hours=config.rag.analysis_lookback_hours,
        )
        if price is None:
```

を次へ:

```python
        price = analysis_store.aggregate(
            pair_cfg.symbol,
            hours=config.rag.analysis_lookback_hours,
        )
        tech_fallback = price is None
        if price is None:
```

(2) 成功 return。現状:

```python
        return signal, macro_ctx
    except Exception as e:
        logger.error(f"Failed to process {pair_cfg.display_name}: {e}", exc_info=True)
        return e
```

を次へ:

```python
        signal.tv_recommendation = tv_summary.recommendation if tv_summary else ""
        return PairAnalysisOutcome(
            signal=signal, macro_ctx=macro_ctx, tech_fallback=tech_fallback,
        )
    except Exception as e:
        logger.error(f"Failed to process {pair_cfg.display_name}: {e}", exc_info=True)
        return PairAnalysisError(pair=pair_cfg.symbol, error=e)
```

- [ ] **Step 3d: `bounded` と `_phase_analyze_pairs` を更新**

`_phase_analyze_pairs` の `bounded` 関数〜 return までを次へ置き換える。現状:

```python
    async def bounded(pair_cfg):
        async with semaphore:
            return await _process_pair(
                pair_cfg, config, position_mgr, store, price_store, analysis_store, llm_price,
                price_provider=price_provider,
                forecast_store=forecast_store,
            )

    results = await asyncio.gather(
        *[bounded(p) for p in config.tradeable_instruments],
        return_exceptions=True,
    )

    signals_with_macro = [r for r in results if not isinstance(r, Exception)]
    errors             = [r for r in results if isinstance(r, Exception)]
    signals    = [s for s, _ in signals_with_macro]
    macro_ctxs = {s.pair: m for s, m in signals_with_macro}
    if errors:
        logger.warning(f"{len(errors)} pair(s) failed during analysis.")
    return signals, macro_ctxs
```

を次へ:

```python
    async def bounded(pair_cfg):
        async with semaphore:
            try:
                return await _process_pair(
                    pair_cfg, config, position_mgr, store, price_store, analysis_store,
                    llm_price,
                    price_provider=price_provider,
                    forecast_store=forecast_store,
                )
            except Exception as e:  # noqa: BLE001 — _process_pair 捕捉漏れの防御網
                logger.error(
                    f"[ANALYZE] {pair_cfg.symbol} unexpected: {e}", exc_info=True,
                )
                return PairAnalysisError(pair=pair_cfg.symbol, error=e)

    results = await asyncio.gather(
        *[bounded(p) for p in config.tradeable_instruments],
        return_exceptions=True,
    )

    outcomes: list[PairAnalysisOutcome] = []
    data_health: list[str] = []
    for r in results:
        if isinstance(r, PairAnalysisOutcome):
            outcomes.append(r)
            if r.tech_fallback:
                data_health.append(
                    f"{r.signal.pair} スナップショット未取得(即時分析fallback)"
                )
        elif isinstance(r, PairAnalysisError):
            data_health.append(f"{r.pair} 分析失敗")
            logger.warning(f"[ANALYZE] {r.pair} failed: {r.error}")
        elif isinstance(r, Exception):
            data_health.append("ペア分析で想定外エラー")
            logger.error(f"[ANALYZE] unexpected gather exception: {r}")

    signals = [o.signal for o in outcomes]
    macro_ctxs = {o.signal.pair: o.macro_ctx for o in outcomes}
    if data_health:
        logger.warning(f"{len(data_health)} pair-analysis issue(s).")
    return signals, macro_ctxs, data_health
```

`_phase_analyze_pairs` の戻り値型注釈も更新する。現状 `-> tuple[list, dict[str, str]]:` を `-> tuple[list, dict[str, str], list[str]]:` へ。

- [ ] **Step 3e: `trading_cycle` の Phase 3 呼び出しを更新**

`src/cycles/trading.py` の `trading_cycle` 内、Phase 3 呼び出し。現状:

```python
    # Phase 3: 並列ペア分析
    signals, macro_ctxs = await _phase_analyze_pairs(
        config, position_mgr, store, price_store, analysis_store, llm_price, price_provider,
        forecast_store=forecast_store,
    )
```

を次へ:

```python
    # Phase 3: 並列ペア分析
    signals, macro_ctxs, data_health = await _phase_analyze_pairs(
        config, position_mgr, store, price_store, analysis_store, llm_price, price_provider,
        forecast_store=forecast_store,
    )
```

(`data_health` は Task 7 で `notify_cycle_summary` に渡す。本タスクでは未使用のまま。)

- [ ] **Step 3f: `test_trading_cycle_halt.py` の `_phase_analyze_pairs` モックを更新**

`tests/test_trading_cycle_halt.py` には `_phase_analyze_pairs` を 2 箇所モックしている (`AsyncMock(return_value=([], {}))`)。両方を `AsyncMock(return_value=([], {}, []))` に変更する:

```python
    analyze_mock = AsyncMock(return_value=([], {}, []))
```

(`test_trading_cycle_skips_phase_analyze_when_halted` と `test_trading_cycle_runs_phase_analyze_when_not_halted` の 2 箇所。)

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_trading_cycle_summary.py tests/test_trading_cycle_halt.py -v`
Expected: PASS (全テスト)

- [ ] **Step 5: コミット**

```bash
git add src/cycles/trading.py tests/test_trading_cycle_summary.py tests/test_trading_cycle_halt.py
git commit -m "feat: 分析Phaseで失敗ペアを特定しdata_healthを収集"
```

---

### Task 6: `_adjust_signal_with_rag` が補正注記を返す

**Files:**
- Modify: `src/cycles/trading.py` (`_adjust_signal_with_rag`)
- Test: `tests/test_trading_cycle_helpers.py` (追記)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_trading_cycle_helpers.py` の `# ── _phase_close_sl_tp` コメントの直前 (= `_adjust_signal_with_rag` テスト群の末尾) に追記:

```python
@pytest.mark.asyncio
async def test_adjust_signal_with_rag_returns_note_when_adjusted(monkeypatch):
    """action/score を変えたら補正注記を返す。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    monkeypatch.setattr("src.cycles.trading.compute_rag_adjustment", lambda **k: 0.20)
    sig = _make_signal(score=0.30, action="buy")
    cfg = RagAdjustmentConfig(enabled=True)

    note = await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(_FakeDirectional()), _fake_embed, deadband=0.15,
    )
    assert note != ""
    assert "→" in note
    assert sig.combined_score == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_adjust_signal_with_rag_returns_empty_when_no_change(monkeypatch):
    """補正が 0 のときは空文字を返す。"""
    from src.signals.rag_adjustment import RagAdjustmentConfig
    from src.trading_cycle import _adjust_signal_with_rag

    monkeypatch.setattr("src.cycles.trading.compute_rag_adjustment", lambda **k: 0.0)
    sig = _make_signal(score=0.30, action="buy")
    cfg = RagAdjustmentConfig(enabled=True)

    note = await _adjust_signal_with_rag(
        sig, cfg, _FakeStoreWithDirectional(_FakeDirectional()), _fake_embed, deadband=0.15,
    )
    assert note == ""
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_trading_cycle_helpers.py -k adjust_signal_with_rag -v`
Expected: FAIL — `test_adjust_signal_with_rag_returns_note_when_adjusted` が `assert note != ""` で失敗 (現状の戻り値は `None`)

- [ ] **Step 3: `_adjust_signal_with_rag` を更新**

`src/cycles/trading.py` の `_adjust_signal_with_rag` を次の全文へ置き換える:

```python
async def _adjust_signal_with_rag(
    sig,
    rag_cfg: RagAdjustmentConfig,
    store: VectorStore,
    embed_fn_adj,
    deadband: float,
) -> str:
    """方向別 RAG の過去成績をもとにシグナルスコアを補正し、必要なら action も再判定する。

    戻り値: action または score を変えた場合は補正注記文字列、変えない場合は ""。
    """
    if not (rag_cfg.enabled and sig.action != "hold"):
        return ""
    try:
        query_embedding = await embed_fn_adj(sig.detail_reason)
        same_dir = "bullish" if sig.combined_score > 0 else "bearish"
        opposite_dir = "bearish" if sig.combined_score > 0 else "bullish"
        same_hits = store.directional.query(
            query_embedding=query_embedding, direction=same_dir,
            top_k=rag_cfg.search_top_n, phase_filter="complete",
        )
        opposite_hits = store.directional.query(
            query_embedding=query_embedding, direction=opposite_dir,
            top_k=rag_cfg.search_top_n, phase_filter="complete",
        )
        adjustment = compute_rag_adjustment(
            combined_score=sig.combined_score,
            same_direction_hits=same_hits,
            opposite_direction_hits=opposite_hits,
            config=rag_cfg,
        )
    except Exception as e:
        logger.warning(f"[RAG ADJ] {sig.pair}: failed — {e}")
        return ""

    adjusted_score = sig.combined_score + adjustment
    if adjusted_score == sig.combined_score:
        return ""

    old_score = sig.combined_score
    old_action = sig.action
    logger.info(
        f"[RAG ADJ] {sig.pair}: combined={old_score:+.3f} → adjusted={adjusted_score:+.3f}"
    )
    if adjusted_score > deadband:
        sig.action = "buy"
    elif adjusted_score < -deadband:
        sig.action = "sell"
    else:
        sig.action = "hold"
    sig.combined_score = round(adjusted_score, 4)

    note = f"score {old_score:+.3f}→{sig.combined_score:+.3f}"
    if sig.action != old_action:
        note += f", {old_action}→{sig.action}"
    return note
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_trading_cycle_helpers.py -k adjust_signal_with_rag -v`
Expected: PASS (既存4 + 新規2 = 6 tests)

- [ ] **Step 5: コミット**

```bash
git add src/cycles/trading.py tests/test_trading_cycle_helpers.py
git commit -m "feat: _adjust_signal_with_rag がRAG補正注記を返す"
```

---

### Task 7: 実行 Phase — `SignalOutcome` 収集と集約通知

`_execute_one_signal` を `SignalOutcome` を返すよう変更し、`_phase_execute_signals` が `outcomes` を集約、`trading_cycle` が `notify_cycle_summary` を呼ぶ。

**Files:**
- Modify: `src/cycles/trading.py` (import + `_execute_one_signal` + `_phase_execute_signals` + `trading_cycle` halt分岐/Phase 4b)
- Test: `tests/test_trading_cycle_summary.py` (追記)
- Modify: `tests/test_trading_cycle_halt.py` (`_phase_execute_signals` モック + notifier モック)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_trading_cycle_summary.py` の末尾に追記:

```python
def _exec_signal(action: str = "buy") -> MagicMock:
    """_execute_one_signal / _phase_execute_signals 用の signal モック。"""
    s = MagicMock()
    s.pair = "USDJPY=X"
    s.action = action
    s.confidence = 0.75
    s.combined_score = 0.32
    s.signal_reason = "rates higher"
    s.detail_reason = "detail"
    s.entry_price = 159.0
    s.stop_loss = 158.0
    s.take_profit = 161.0
    s.position_size = 1000.0
    s.predicted_direction = "bullish"
    s.tv_recommendation = "BUY"
    s.news = MagicMock(sentiment_score=0.12)
    s.price = MagicMock(bias_score=0.37)
    return s


def _exec_config(notify_on_cycle_summary: bool = True) -> MagicMock:
    c = MagicMock()
    c.notifier.notify_on_cycle_summary = notify_on_cycle_summary
    c.notifier.notify_on_order_open = True
    c.notifier.notify_on_signal_skipped = True
    return c


@pytest.mark.asyncio
async def test_execute_one_signal_atr_failure_skipped_with_atr_reason(monkeypatch):
    """ATR SL/TP 失敗で hold 降格 → status=skipped, reason は ATR 理由 (hold文言にしない)。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult

    monkeypatch.setattr("src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: None)
    sig = _exec_signal(action="buy")
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.skipped("hold (発注対象外)")

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "skipped"
    assert outcome.action == "buy"
    assert outcome.reason == "ATR SL/TP calculation failed"
    assert "発注対象外" not in outcome.reason


@pytest.mark.asyncio
async def test_execute_one_signal_executed_returns_outcome_with_order(monkeypatch):
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "executed"
    assert outcome.order is order
    assert outcome.news_score == 0.12
    assert outcome.tech_score == 0.37
    assert outcome.tv_recommendation == "BUY"


@pytest.mark.asyncio
async def test_execute_one_signal_fallback_fires_old_notification(monkeypatch):
    """notify_on_cycle_summary=False なら旧 notify_order_opened が発火する。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)
    notifier = MagicMock()
    notifier.notify_order_opened = AsyncMock()

    await _execute_one_signal(
        sig, "", _exec_config(notify_on_cycle_summary=False), MagicMock(), broker,
        notifier, MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    notifier.notify_order_opened.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_one_signal_no_old_notification_when_summary_enabled(monkeypatch):
    """notify_on_cycle_summary=True なら旧 notify_order_opened は発火しない。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult
    from src.trading.position_manager import Order

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="buy")
    order = Order.new("USDJPY=X", "buy", 159.0, 158.0, 161.0, 1000.0)
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.executed(order)
    notifier = MagicMock()
    notifier.notify_order_opened = AsyncMock()

    await _execute_one_signal(
        sig, "", _exec_config(notify_on_cycle_summary=True), MagicMock(), broker,
        notifier, MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    notifier.notify_order_opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_one_signal_broker_rejection_stays_rejected(monkeypatch):
    """MT5 拒否 (ExecutionResult.rejected) は status=rejected のまま (skipped に落とさない)。"""
    from src.cycles.trading import _execute_one_signal
    from src.trading.broker_adapter import ExecutionResult

    monkeypatch.setattr(
        "src.cycles.trading._apply_atr_sltp_to_signal", lambda *a, **k: MagicMock())
    sig = _exec_signal(action="sell")
    broker = MagicMock()
    broker.execute_signal.return_value = ExecutionResult.rejected(
        "発注拒否 (broker): retcode=10016 Invalid stops")

    outcome = await _execute_one_signal(
        sig, "", _exec_config(), MagicMock(), broker,
        MagicMock(), MagicMock(), MagicMock(), None, MagicMock(), AsyncMock(), MagicMock(),
    )
    assert outcome.status == "rejected"
    assert "retcode=10016" in outcome.reason


@pytest.mark.asyncio
async def test_halt_cycle_sends_halt_summary(tmp_path, monkeypatch):
    """halt 中サイクルは halted=True の CycleSummaryEvent を1回送る。"""
    from src.cycles.trading import trading_cycle
    from src.persistence import halt_state

    halt_state.trigger_manual(tmp_path, reason="test")
    notifier = MagicMock(notify_cycle_summary=AsyncMock())
    monkeypatch.setattr(
        "src.cycles.trading._build_trading_runtime",
        lambda c: (MagicMock(), MagicMock(), notifier,
                   MagicMock(model_name="m"), MagicMock(model_name="r")),
    )
    monkeypatch.setattr("src.cycles.trading._phase_close_sl_tp", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.cycles.trading._finalize_closed_orders", AsyncMock(return_value=None))
    monkeypatch.setattr("src.cycles.trading._review_hold_decisions", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.cycles.trading._phase_review_open_positions", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.cycles.trading.is_market_open", lambda *a, **k: True)
    monkeypatch.setattr("src.cycles.trading.local_now", lambda c: datetime(2026, 5, 19, 17, 30))
    monkeypatch.setattr("src.cycles.trading.make_embed_fn", lambda c: lambda x: [])
    monkeypatch.setattr("src.cycles.trading.print_run_summary", lambda **kw: None)

    config = MagicMock()
    config.state_dir = tmp_path
    config.mode = "paper"
    config.notifier.notify_on_cycle_summary = True

    pm = MagicMock()
    pm.get_account_state.return_value = MagicMock(balance=10000.0)
    await trading_cycle(
        config, pm, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        price_provider=MagicMock(), session_store=MagicMock(),
    )
    notifier.notify_cycle_summary.assert_awaited_once()
    event = notifier.notify_cycle_summary.call_args.args[0]
    assert event.halted is True
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `pytest tests/test_trading_cycle_summary.py -k "execute_one_signal or halt_cycle" -v`
Expected: FAIL — `_execute_one_signal` がまだ `Order | None` を返す / `notify_cycle_summary` 未呼び出し

- [ ] **Step 3a: trading.py の notifier import を更新**

`src/cycles/trading.py` の `from src.notifications.notifier import (...)` を次へ:

```python
from src.notifications.notifier import (
    CycleSummaryEvent,
    OrderClosedEvent,
    OrderOpenedEvent,
    SignalOutcome,
    SignalSkippedEvent,
    create_notifier,
)
```

- [ ] **Step 3b: `_execute_one_signal` を更新**

`src/cycles/trading.py` の `_execute_one_signal` を次の全文へ置き換える:

```python
async def _execute_one_signal(
    sig,
    macro_ctx: str,
    config: AppConfig,
    position_mgr: PositionManager,
    broker,
    notifier,
    store: VectorStore,
    price_store: PriceStore,
    session_store,
    adaptive_store: AdaptiveParamsStore,
    embed_fn_adj,
    price_provider: PriceProvider | None,
) -> SignalOutcome:
    """1シグナルの発注処理を実行し、結果を SignalOutcome で返す。"""
    original_action = sig.action  # ATR 降格判定用に退避

    sltp_result = _apply_atr_sltp_to_signal(
        sig, config, position_mgr, price_store, adaptive_store,
        price_provider=price_provider,
    )

    # ATR SL/TP 算出に失敗した場合はエントリーしない (SL/TP=0 で約定すると即決済になる)
    if sltp_result is None and sig.action in ("buy", "sell"):
        logger.warning(
            f"[SIGNAL] {sig.pair}: ATR SL/TP unavailable — skipping entry"
        )
        sig.action = "hold"
        sig.signal_reason = "ATR SL/TP calculation failed"

    # ATR 降格: 元 buy/sell が ATR 処理 (SL/TP失敗 or R:R不足) で hold 化された
    atr_demoted = original_action in ("buy", "sell") and sig.action == "hold"

    result = broker.execute_signal(sig, position_mgr, macro_context=macro_ctx)

    if atr_demoted:
        status = "skipped"
        block_action = original_action
        reason = sig.signal_reason  # ATR レイヤが設定した具体的理由
    elif result.is_executed:
        status = result.outcome
        block_action = sig.action
        reason = sig.signal_reason
    else:
        status = result.outcome
        block_action = sig.action
        reason = result.reason

    outcome = SignalOutcome(
        pair=sig.pair,
        action=block_action,
        status=status,
        confidence=sig.confidence,
        combined_score=sig.combined_score,
        reason=reason,
        detail_reason=sig.detail_reason,
        news_score=sig.news.sentiment_score,
        tech_score=sig.price.bias_score,
        tv_recommendation=sig.tv_recommendation,
        order=result.order,
    )

    # notify_on_cycle_summary=False のときは旧 per-event 通知へフォールバック
    if not config.notifier.notify_on_cycle_summary:
        if result.is_executed:
            if config.notifier.notify_on_order_open:
                await notifier.notify_order_opened(OrderOpenedEvent(
                    pair=sig.pair,
                    direction=sig.action,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    position_size=sig.position_size,
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                    detail_reason=sig.detail_reason,
                    source="trading",
                    is_scale_in=result.order.is_scale_in,
                ))
        elif config.notifier.notify_on_signal_skipped:
            await notifier.notify_signal_skipped(SignalSkippedEvent(
                pair=sig.pair,
                action=sig.action,
                confidence=sig.confidence,
                signal_reason=sig.signal_reason,
                detail_reason=sig.detail_reason,
                source="trading",
                outcome=result.outcome,
                skip_reason=result.reason,
            ))

    if not result.is_executed:
        return outcome

    order = result.order

    if session_store:
        direction = "bullish" if order.direction == "buy" else "bearish"
        entry_ctx = ""
        if sltp_result:
            entry_ctx = build_entry_context(
                combined_score=sig.combined_score,
                confidence=sig.confidence,
                action=sig.action,
                news_weight=config.trading.news_weight,
                price_weight=config.trading.price_weight,
                news=sig.news,
                price=sig.price,
                sltp=sltp_result,
                macro_context=macro_ctx,
            )
        session_store.create_session(
            session_id=order.order_id,
            pair=order.pair,
            direction=direction,
            entry_price=order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            position_size=order.position_size,
            signal_score=sig.combined_score,
            signal_confidence=sig.confidence,
            macro_context=macro_ctx,
            analysis_summary=entry_ctx or sig.detail_reason,
            opened_at=order.opened_at,
            atr_value=sltp_result.atr_value if sltp_result else None,
            sl_atr_mult=sltp_result.sl_atr_mult if sltp_result else None,
            tp_atr_mult=sltp_result.tp_atr_mult if sltp_result else None,
            computed_sl=sltp_result.computed_sl if sltp_result else None,
            computed_tp=sltp_result.computed_tp if sltp_result else None,
            llm_sl=sltp_result.llm_sl if sltp_result else None,
            llm_tp=sltp_result.llm_tp if sltp_result else None,
            key_support=sltp_result.key_support if sltp_result else None,
            key_resistance=sltp_result.key_resistance if sltp_result else None,
        )
        await record_trade_entry(store, embed_fn_adj, order, sig)
    return outcome
```

- [ ] **Step 3c: `_phase_execute_signals` を更新**

`src/cycles/trading.py` の `_phase_execute_signals` の本体 (`executed_orders = []` 以降〜 `return executed_orders`) を次へ置き換える。戻り値型注釈も `-> list:` から `-> tuple[list, list]:` へ変更する:

```python
    executed_orders: list = []
    outcomes: list[SignalOutcome] = []
    for sig in signals:
        rag_note = await _adjust_signal_with_rag(sig, rag_cfg, store, embed_fn_adj, deadband)

        if sig.action != "hold":
            outcome = await _execute_one_signal(
                sig, macro_ctxs.get(sig.pair, ""),
                config, position_mgr, broker, notifier, store, price_store,
                session_store, adaptive_store, embed_fn_adj,
                price_provider=price_provider,
            )
            outcome.rag_note = rag_note
            outcomes.append(outcome)
            if outcome.order is not None:
                executed_orders.append(outcome.order)
        else:
            outcome = SignalOutcome(
                pair=sig.pair,
                action="hold",
                status="hold",
                confidence=sig.confidence,
                combined_score=sig.combined_score,
                reason=sig.signal_reason,
                detail_reason=sig.detail_reason,
                news_score=sig.news.sentiment_score,
                tech_score=sig.price.bias_score,
                tv_recommendation=sig.tv_recommendation,
                rag_note=rag_note,
            )
            outcomes.append(outcome)
            if (not config.notifier.notify_on_cycle_summary
                    and config.notifier.notify_on_signal_skipped):
                await notifier.notify_signal_skipped(SignalSkippedEvent(
                    pair=sig.pair,
                    action="hold",
                    confidence=sig.confidence,
                    signal_reason=sig.signal_reason,
                    detail_reason=sig.detail_reason,
                    predicted_direction=sig.predicted_direction,
                    source="trading",
                ))
            hold_store.save_hold(sig.pair, sig)
    return executed_orders, outcomes
```

- [ ] **Step 3d: `trading_cycle` の halt 分岐と Phase 4b を更新**

(1) halt 分岐。現状:

```python
        await _finalize_closed_orders(
            timeout_closed, config, store, embed_fn, llm_reflect,
            adaptive_store, session_store, log_source="[REFLECT/TIMEOUT_HALT]",
        )
        return
```

を次へ:

```python
        await _finalize_closed_orders(
            timeout_closed, config, store, embed_fn, llm_reflect,
            adaptive_store, session_store, log_source="[REFLECT/TIMEOUT_HALT]",
        )
        if config.notifier.notify_on_cycle_summary:
            await notifier.notify_cycle_summary(CycleSummaryEvent(
                cycle_time=run_start, outcomes=[], halted=True,
            ))
        return
```

(2) Phase 4b。現状:

```python
    # Phase 4b: 新規シグナル発注
    executed_orders = await _phase_execute_signals(
        signals, macro_ctxs, config, position_mgr, broker, notifier, store, price_store,
        hold_store, session_store, adaptive_store, embed_fn, price_provider,
    )
```

を次へ:

```python
    # Phase 4b: 新規シグナル発注
    executed_orders, outcomes = await _phase_execute_signals(
        signals, macro_ctxs, config, position_mgr, broker, notifier, store, price_store,
        hold_store, session_store, adaptive_store, embed_fn, price_provider,
    )

    # Phase 4b 後: 集約サマリーを1通送信
    if config.notifier.notify_on_cycle_summary:
        await notifier.notify_cycle_summary(CycleSummaryEvent(
            cycle_time=run_start, outcomes=outcomes, data_health=data_health,
        ))
```

- [ ] **Step 3e: `test_trading_cycle_halt.py` を更新**

`tests/test_trading_cycle_halt.py` を3点変更する。

(1) ファイル冒頭の import 直後に runtime モックヘルパーを追加:

```python
def _runtime_mock(c):
    """_build_trading_runtime の戻り値 (broker, adaptive, notifier, llm_price, llm_reflect)。"""
    return (
        MagicMock(), MagicMock(),
        MagicMock(notify_cycle_summary=AsyncMock()),
        MagicMock(model_name="m"), MagicMock(model_name="r"),
    )
```

(2) `_build_trading_runtime` を monkeypatch している4箇所すべてを、ヘルパー利用へ置き換える:

```python
    monkeypatch.setattr("src.cycles.trading._build_trading_runtime", _runtime_mock)
```

(3) `_phase_execute_signals` を monkeypatch している2箇所の戻り値を `[]` から `([], [])` へ:

```python
    execute_mock = AsyncMock(return_value=([], []))
```

および

```python
    monkeypatch.setattr(
        "src.cycles.trading._phase_execute_signals",
        AsyncMock(return_value=([], [])),
    )
```

- [ ] **Step 4: テストが通ることを確認**

Run: `pytest tests/test_trading_cycle_summary.py tests/test_trading_cycle_halt.py -v`
Expected: PASS (全テスト)

- [ ] **Step 5: 全スイート回帰確認**

Run: `pytest -q`
Expected: PASS (既存スイート + 新規テストすべて green)

- [ ] **Step 6: コミット**

```bash
git add src/cycles/trading.py tests/test_trading_cycle_summary.py tests/test_trading_cycle_halt.py
git commit -m "feat: 取引サイクルの発注判断を1通の集約通知にまとめる"
```

---

## 完了条件

- 全タスクのテストが green。
- `pytest -q` で既存スイートに回帰なし。
- `notify_on_cycle_summary=True` (既定) で取引サイクルが1通の集約サマリーを送信。
- `notify_on_cycle_summary=False` で旧 per-event 通知へ切り戻る。
- 決済通知 (`notify_order_closed`) は従来どおり個別即時 — 変更なし。

## 実装後の運用 (ユーザー作業 — コード対象外)

- `feat/cycle-summary-notification` を `main` へマージ。
- stick PC へデプロイ + デーモン再起動。
- `git push origin main`。
