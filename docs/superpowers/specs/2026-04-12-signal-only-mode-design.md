# Signal Only Mode — Design Spec

## Goal

`trading_mode: "signal_only"` を追加し、LLM 取引判定のシグナルを通知しつつ、内部的には paper 同等の自動運用で RAG を育てる。実際の発注は TradingView ブローカー連携で手動実行し、shadow API で実ポジションを記録・振り返る。

## Architecture

```
取引サイクル
├── Internal PositionManager (自動・非通知)
│   paper_broker で自動運用 → SL/TP close → reflection → RAG 蓄積
│   ログ出力: [INTERNAL] プレフィックス
│
├── Signal Notification (signal_only_broker)
│   シグナル → SignalRecommendationEvent (Discord Webhook)
│   SL/TP 到達 → SLTPAlertEvent
│   Layer 1-3 推奨 → ReviewAdvisoryEvent
│   ポートフォリオガード → 情報付加のみ (ブロックしない)
│
└── Shadow PositionManager (手動・REST API)
    /shadow/open → 実ポジション登録
    /shadow/close → 決済記録 → reflection → RAG 蓄積
    /shadow/list → 一覧
    /shadow/balance → 残高補正
```

### Dual PositionManager

| 系統 | 用途 | 管理 | 通知 | Reflection |
|---|---|---|---|---|
| **internal** | RAG 学習データ蓄積 | 自動 (paper_broker) | なし (ログのみ) | 自動生成 → RAG |
| **shadow** | 実取引記録 | 手動 (shadow API) | なし (close 時のみログ) | /shadow/close 後に生成 → RAG |

internal は既存の paper_broker をそのまま使う。state_store のパスを分離して shadow と競合させない。

## SignalOnlyBrokerAdapter

新ファイル: `src/trading/signal_only_broker.py`

```python
class SignalOnlyBrokerAdapter(BrokerAdapter):
    def __init__(self, shadow_position_mgr, notifier, ...):
        # shadow PositionManager への参照を保持 (ガード判定・既存ポジ数の取得に使用)

    def execute_signal(self, signal, position_mgr, macro_context=""):
        # NOTE: position_mgr 引数は internal (BrokerAdapter I/F 互換)。使用しない。
        # 1. ポートフォリオガード判定 (self._shadow_mgr の open_positions 対象)
        #    → 引っかかる場合は portfolio_warning を設定
        # 2. 同一ペア既存 shadow ポジション数を取得
        # 3. SignalRecommendationEvent を通知
        # 4. return None (注文は発行しない)

    def check_and_close_positions(self, open_positions, current_prices, position_mgr):
        # NOTE: 引数の open_positions は internal。shadow positions は self._shadow_mgr から取得。
        # shadow positions の SL/TP 到達を検出
        # → SLTPAlertEvent を通知
        # → close はしない
        # return []
```

`create_broker("signal_only", ...)` で生成。

## Notification Events

### SignalRecommendationEvent

```python
@dataclass
class SignalRecommendationEvent:
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
    portfolio_warning: str      # ガード注記 (空文字なら制限なし)
    existing_positions: int     # 同一ペアの既存 shadow ポジション数
    source: str                 # "trading"
```

### SLTPAlertEvent

```python
@dataclass
class SLTPAlertEvent:
    pair: str
    direction: str
    order_id: str
    entry_price: float
    current_price: float
    trigger: str                # "stop_loss" | "take_profit"
    unrealized_pnl: float
```

### ReviewAdvisoryEvent

```python
@dataclass
class ReviewAdvisoryEvent:
    pair: str
    direction: str
    order_id: str
    close_reason: str           # "reversal" | "timeout" | "profit_lock"
    detail: str
    current_price: float
```

## Shadow REST API

`src/api/routes/shadow.py` — signal_only モード時のみ有効。`X-API-Key` 認証。

### POST /shadow/open

```json
// Request
{
  "pair": "USDJPY=X",
  "direction": "buy",
  "entry_price": 150.200,
  "position_size": 5000,
  "stop_loss": 149.800,
  "take_profit": 151.200,
  "signal_reason": "score=+0.32"
}

// Response 200
{
  "order_id": "abc-123-...",
  "pair": "USDJPY=X",
  "direction": "buy",
  "entry_price": 150.200
}
```

バリデーション: pair が instruments に存在すること、SL/TP が方向と整合すること。1ペア複数ポジ可。

### POST /shadow/close/{order_id}

```json
// Request
{
  "close_price": 151.100,
  "close_reason": "take_profit"
}

// Response 200
{
  "order_id": "abc-123-...",
  "realized_pnl": 4500.0,
  "balance": 504500.0
}
```

`close_reason`: "take_profit" | "stop_loss" | "manual" | "reversal" | "timeout" | "profit_lock"

close 後にバックグラウンドで LLM reflection を生成し RAG に蓄積。レスポンスは即座に返す。

### GET /shadow/list

```json
// Response 200
{
  "balance": 500000.0,
  "open_positions": [
    {
      "order_id": "abc-123-...",
      "pair": "USDJPY=X",
      "direction": "buy",
      "entry_price": 150.200,
      "stop_loss": 149.800,
      "take_profit": 151.200,
      "position_size": 5000,
      "opened_at": "2026-04-12T10:00:00"
    }
  ]
}
```

### POST /shadow/balance

```json
// Request
{"balance": 500000}

// Response 200
{"balance": 500000.0, "previous": 495000.0}
```

## Shadow TUI (Interactive Mode)

```
> shadow
=== Shadow Mode ===

[Pending Signals]
  1. USDJPY=X BUY  score=+0.32 entry=150.200 SL=149.800 TP=151.200 lot=5000
  2. EURJPY=X SELL score=-0.28 entry=163.500 SL=164.200 TP=162.100 lot=3000

[Open Positions]
  3. USDJPY=X BUY  entry=149.800 SL=149.400 TP=151.000 PnL=+40.0

Choose action:
  <番号> open  — シグナルをポジション登録
  <番号> close — ポジション決済記録
  balance       — 残高補正
  q             — 終了

> 1 open
→ Confirm: USDJPY=X BUY 150.200 lot=5000 SL=149.800 TP=151.200? [Y/n/edit]
> y
✓ Registered (order_id: abc123)
```

`edit` でロット・SL/TP を対話的に修正可能。直近シグナルはメモリに保持（再起動でクリア）。

## Trading Cycle Changes

### Phase 4a: _phase_review_open_positions

`advisory_only: bool` パラメータを追加。signal_only のとき True。

- advisory_only=False (paper/live): Layer 1-3 判定 → close_position → reflection
- advisory_only=True (signal_only): Layer 1-3 判定 → ReviewAdvisoryEvent 通知のみ

対象は **shadow positions** のみ。internal positions は paper_broker の自動決済に任せる。

### Internal PositionManager

取引サイクルのメインフローは internal PositionManager で paper_broker 同等に動作:

- Phase 1: SL/TP クローズ (自動)
- Phase 1.5: reflection → RAG 蓄積
- Phase 3: シグナル生成
- Phase 4b: internal PositionManager に open → ログ出力 `[INTERNAL]`

通知は一切発火しない。`create_notifier(enabled=False)` 相当の NullNotifier を使う。

### Signal Notification

Phase 4b でシグナルが確定した後、signal_only_broker 経由で通知:
- BUY/SELL → SignalRecommendationEvent
- HOLD → 既存の SignalSkippedEvent (predicted_direction 付き)

## Config Changes

`TradingConfig` に追加:
- `trading_mode: "signal_only"` を有効値として追加 ("paper" | "signal_only" | "live")

`settings.yaml.example` に signal_only の設定例を追加。

## State Separation

```
data/state/
  positions.json          # internal (自動運用)
  trades.json             # internal
  shadow_positions.json   # shadow (手動記録)
  shadow_trades.json      # shadow
  adaptive_params.yaml    # 共有
```

両方の PositionManager が同じ `initial_balance` で開始するが、shadow 側は `/shadow/balance` で実残高に補正可能。

## Decisions

| 項目 | 決定 |
|---|---|
| Layer 1 反転推奨 | 全ポジ一斉通知 |
| 新規シグナル抑制 | 常に通知。判断はユーザー |
| Reflection 生成 | shadow close 後に生成 + internal 自動生成 |
| ポートフォリオガード | 情報付加のみ、ブロックしない |
| Discord UX | finance は REST API + Webhook ペイロード提供。UI は discord_bot 側 |
| Internal 動作 | paper 同等の自動運用。通知なし、ログのみ (`[INTERNAL]`) |

## Implementation Order

1. State 分離 (shadow_positions.json / shadow_trades.json)
2. SignalOnlyBrokerAdapter + create_broker 拡張
3. Internal PositionManager + NullNotifier 統合
4. 通知イベント 3 種 (SignalRecommendation, SLTPAlert, ReviewAdvisory)
5. discord_notifier にハンドラ追加
6. Phase 4a advisory 分岐
7. Shadow REST API (/shadow/open, close, list, balance)
8. Shadow TUI インタラクティブモード
9. テスト
