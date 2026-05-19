# 取引サイクル集約通知 (Cycle Summary Notification) 設計書

作成日: 2026-05-19
ステータス: 設計確定 (実装計画待ち)

## 目的

取引サイクルの Discord 通知を「シグナル1件ごとに1通」から「1サイクル1通の集約サマリー」へ変更する。
1回の取引サイクルで生成された全ペアの発注判断 (EXECUTED / HOLD / 拒否 / 失敗 / スキップ) を、
1つの読みやすいメッセージにまとめて送信する。

## 背景・現状

現在、取引サイクル ([src/cycles/trading.py](../../../src/cycles/trading.py)) の通知は各 Phase に
散在しており、**シグナル1件ごとに個別の Discord メッセージ**を発火する:

- `_execute_one_signal` → `notify_order_opened` (発注成功1件ごと)
- `_execute_one_signal` → `notify_signal_skipped` (拒否/失敗1件ごと)
- `_phase_execute_signals` の hold 分岐 → `notify_signal_skipped` (HOLD 1件ごと)

ペアが2つでもサイクルごとに2〜3通が届き、サイクル全体の結果を1つの通知で把握できない。

`notify_order_opened` / `notify_signal_skipped` は調査の結果、**取引サイクルからのみ**呼ばれている
(`notify_order_closed` / `notify_price_alert` は exit_check / price_monitor / api / cli からも使用)。

## スコープ

### 対象 (集約する)

- 取引サイクル Phase 4b の新規エントリー判断通知 (EXECUTED / HOLD / 拒否 / 失敗 / スキップ)
- halt 中サイクル (新規発注分析をスキップした場合) の簡易サマリー

### 非対象 (即時通知のまま維持)

- 決済通知 (`notify_order_closed`) — SL/TP 到達・レビュー早期決済。実損益確定の重要イベントのため即時。
- hard halt / emergency stop の発動通知 — 別系統の即時アラート。
- 価格急変動通知 (`notify_price_alert`) — price_monitor ジョブの別系統。
- 市場休場時 — 従来どおり**無音** (何も生成されないため通知しない)。

## アーキテクチャ

採用アプローチ: **収集オブジェクト + サイクル末尾で1回通知**。

Phase 4b が各シグナルを処理する際、Discord 通知を即時発火せず、各ペアの結果を `SignalOutcome`
として蓄積する。Phase 4b 完了後に `CycleSummaryEvent` を作り、`notify_cycle_summary()` を1回だけ呼ぶ。

この設計により:

- notifier はステートレスを保てる (バッファリング不要)。
- `SignalOutcome` が取引判断結果の明確なインターフェースになる。
- 変更範囲が Phase 4b と通知層に限定される。
- テストが容易 (`CycleSummaryEvent` 1つを渡して整形結果を検証)。

却下した代替案:

- **notifier 内部バッファリング** (begin_cycle/end_cycle) — notifier がステートフル化し、
  隠れたバッファリングでテスト困難。現在のシンプルな notifier 設計を壊す。
- **reporter (print_run_summary) に Discord 送信を追加** — reporter は skip/reject/fail の
  outcome/reason を持たず結局データ配線が必要。コンソール描画モジュールに Discord I/O を
  混ぜると責務が崩れる。

## データ構造

### `PairAnalysisOutcome` / `PairAnalysisError` (新規)

`_process_pair` の戻り値。tuple ではなく dataclass にして将来のフィールド追加に耐える。
定義場所: [src/cycles/trading.py](../../../src/cycles/trading.py)。

```python
@dataclass
class PairAnalysisOutcome:
    signal: TradeSignal
    macro_ctx: str
    tech_fallback: bool = False  # 蓄積スナップショットがなく即時 Ollama 分析へ fallback した


@dataclass
class PairAnalysisError:
    pair: str          # 失敗したペアの symbol
    error: Exception   # 元の例外
```

現行コードでは `_process_pair` が例外を捕捉して bare `Exception` を返しており、
`_phase_analyze_pairs` 側 (`errors = [r for r in results if isinstance(r, Exception)]`) は
**どのペアが失敗したか復元できない**。本設計は失敗ペアを `data_health` に出すため、
`_process_pair` はエラー時に bare `Exception` ではなく `PairAnalysisError(pair, error)` を返す。
`bounded(pair_cfg)` は `_process_pair` から例外がエスケープした場合も
`PairAnalysisError(pair_cfg.symbol, e)` へ包んで返す (防御的多層化)。
これにより `asyncio.gather` の結果は `PairAnalysisOutcome` か `PairAnalysisError` のみになり、
失敗ペアの symbol を常に特定できる。

### `SignalOutcome` (新規)

1シグナルの発注判断結果。集約サマリー整形専用の純粋なデータ構造。
定義場所: [src/notifications/notifier.py](../../../src/notifications/notifier.py)。

```python
@dataclass
class SignalOutcome:
    pair: str
    action: str                  # "buy" | "sell" | "hold"
    status: str                  # "executed"/"hold"/"skipped"/"halted"/"rejected"/"failed"
    confidence: float
    combined_score: float
    reason: str                  # executed/hold→signal_reason / skipped/rejected/failed→ExecutionResult.reason
    detail_reason: str           # ニュース/テクニカル詳細内訳
    news_score: float            # signal.news.sentiment_score — drivers 行
    tech_score: float            # signal.price.bias_score — drivers 行
    tv_recommendation: str = ""  # signal.tv_recommendation — drivers 行 ("" なら非表示)
    rag_note: str = ""           # RAG 補正が action/score を変えたときの注記 ("" なら非表示)
    order: "Order | None" = None # status=="executed" のとき約定 Order (executed_orders 集計に使用)
```

`rag_note` の理由 — `_adjust_signal_with_rag` は `combined_score` と `action` のみを書き換え、
`signal_reason` / `detail_reason` は書き換えない。そのため集約サマリーで score / BUY・HOLD は
RAG 補正後なのに `reason` が補正前のまま、という表示ズレが起きうる。集約通知は「最終判断」を
見せる UI なので、RAG 補正が action または score を変えた場合は補正内容を `rag_note` に持たせ、
ブロックに明示する。`_adjust_signal_with_rag` は戻り値を `None` から `str` (補正注記。
補正なしなら `""`) へ変更し、`_phase_execute_signals` がペアごとに受け取って `SignalOutcome`
へ渡す。注記の例: `"RAG: score -0.023→+0.115, hold→buy"`。

`Order` 型は notifier→trading の実行時依存を避けるため `TYPE_CHECKING` 経由でインポートする
(整形時の属性アクセス `order.entry_price` 等は実行時インポート不要)。

`entry / SL / TP / RR` は executed 時に `order` から取得する (RR = TP距離 ÷ SL距離)。
HOLD ブロックは entry/SL/TP を表示しないため `order` 不要。

### `CycleSummaryEvent` (新規)

`notify_cycle_summary` に渡す通知イベント。
定義場所: [src/notifications/notifier.py](../../../src/notifications/notifier.py)。

```python
@dataclass
class CycleSummaryEvent:
    cycle_time: datetime
    outcomes: list[SignalOutcome]
    halted: bool = False
    data_health: list[str] = field(default_factory=list)  # 問題文字列。空なら Data 行なし
    source: str = "trading"
```

`data_health` は初回実装では `list[str]` (人間可読の問題文字列)。
将来 severity を持たせる場合は `DataHealthItem` (`pair` / `level: info|warn|error` / `message`)
へ拡張する余地を残す。

### `TradeSignal` への追加フィールド

[src/signals/signal_combiner.py](../../../src/signals/signal_combiner.py) の `TradeSignal` に
1フィールド追加:

```python
tv_recommendation: str = ""  # TradingView コンセンサス推奨 (例: "BUY"/"STRONG_SELL")。未取得時 ""
```

`combine_signals` のシグネチャは変更しない。`_process_pair` が `combine_signals` 呼び出し後に
`signal.tv_recommendation = tv_summary.recommendation if tv_summary else ""` を設定する。

## メッセージ整形仕様

`notify_cycle_summary` がプレーンテキストを組み立て、`send()` で送信する
(既存通知と統一。embed は使わない)。

### 通常サイクル

```
🟢 取引サイクル 17:30 JST
結果: 1発注 / 1HOLD / 0拒否 / 0失敗

📈 USDJPY=X BUY EXECUTED
score +0.320 | conf 75% | RR 2.00
entry 159.004 | SL 158.216 | TP 160.580
drivers: News +0.12 / Tech +0.37 / TV BUY
reason: rates higher + tech long alignment

⏸ EURUSD=X HOLD
score -0.023 | conf 30%
drivers: News +0.09 / Tech -0.05 / TV STRONG_SELL
reason: confidence too low, NEWS/PRICE conflict
```

### ヘッダー絵文字

- `🟢` — 全て正常 (拒否・失敗なし、data_health 問題なし)
- `⚠️` — 拒否 / 失敗あり、または data_health 問題あり
- `🛑` — halt サイクル

### ペアブロック絵文字

| status     | 絵文字 | ラベル例           |
|------------|--------|--------------------|
| executed (buy)  | `📈` | `BUY EXECUTED`     |
| executed (sell) | `📉` | `SELL EXECUTED`    |
| hold       | `⏸`  | `HOLD`             |
| rejected   | `🚫` | `BUY REJECTED`     |
| failed     | `❌` | `SELL FAILED`      |
| skipped    | `⏭`  | `BUY SKIPPED`      |

scale-in 約定は `order.is_scale_in` が True のときラベルに `(scale-in)` を付す。

### 結果行カウント

`結果: N発注 / N HOLD / N拒否 / N失敗` を常時表示。
`Nスキップ` (既存ポジション等の skipped) は `N>0` のときだけ末尾に追記する。
per-signal の `halted` (極めて稀。halt サイクルは Phase 4b 前に short-circuit するため) は
スキップ件数に合算する。

### 各ブロックの構成

- **executed**: `score/conf/RR` 行 + `entry/SL/TP` 行 + `drivers:` 行 + `reason:` 行
- **hold**: `score/conf` 行 + `drivers:` 行 + `reason:` 行 (entry/SL/TP は出さない)
- **rejected / failed**: `score/conf` 行 + `reason:` 行 (`reason` は `ExecutionResult.reason`。
  拒否理由を必ず表示する — バグ2 の意図を継承)
- **skipped**: `score/conf` 行 + `reason:` 行

`drivers:` 行は `tv_recommendation` が空文字なら `TV` 部分を省略する
(`drivers: News +0.12 / Tech +0.37`)。

`rag_note` が非空のブロックは `reason:` 行の下に `RAG: <note>` 行を追加し、RAG 補正で
action / score が変わったことを明示する (`reason` 自体は補正前理由のまま残し、補正内容は
`RAG:` 行で示す)。

### Data 行 (問題時のみ)

`CycleSummaryEvent.data_health` が非空のとき、結果行の直下に1行挿入する:

```
🟢 取引サイクル 17:30 JST
結果: 1発注 / 1HOLD / 0拒否 / 0失敗
⚠ Data: USDJPY スナップショット未取得(即時分析fallback) / EURUSD 分析失敗
```

`data_health` が空なら Data 行は出さない。

### halt サイクル

`CycleSummaryEvent.halted=True` のとき:

```
🛑 取引サイクル 17:30 JST
halt 中 — 新規発注分析をスキップ
既存ポジション管理 (timeout 判定) のみ継続
```

### メッセージ長

Discord の `content` 上限は 2000 文字。2ペアで約450文字、5ペアでも約1000文字で問題ない。
万一上限を超える場合は末尾を切り詰め、`(... N件省略)` の注記を付ける。

## 通知フラグの優先順位

[src/config/schema.py](../../../src/config/schema.py) の `NotifierConfig` に
`notify_on_cycle_summary: bool = True` を追加する。

| `notify_on_cycle_summary` | 動作 |
|---------------------------|------|
| `True` (既定) | 集約通知のみ。取引サイクルは `notify_order_opened` / `notify_signal_skipped` を呼ばない |
| `False` | 旧即時通知にフォールバック。`notify_on_order_open` / `notify_on_signal_skipped` に従う |

これにより、集約通知に不具合があれば config 1行で旧挙動へ切り戻せる。

### フォールバックの実装方法

`notify_on_cycle_summary=False` のフォールバックは、`_execute_one_signal` および
`_phase_execute_signals` の hold 分岐で、**その場の live な `sig` / `result` (ExecutionResult) /
`order` から旧通知 (`notify_order_opened` / `notify_signal_skipped`) を発火**する。
`if not config.notifier.notify_on_cycle_summary:` でゲートする。

この方式なら `SignalOutcome` から旧イベントを再構築する必要がなく (ロスレス)、
`SignalOutcome` を集約サマリー専用の純粋な構造のまま保てる。
`SignalOutcome` のビルドは両モードとも常時行う (戻り値として返すため)。

旧メソッド (`notify_order_opened` / `notify_signal_skipped`) と旧 config フラグ
(`notify_on_order_open` / `notify_on_signal_skipped`) は**残置**する。
集約通知が安定したあとの別タスクで削除を検討する (config ファイル側のキー削除を伴うため、
本変更のスコープには含めない)。

## status の分類規律 (バグ2 再発防止)

`SignalOutcome.status` は `ExecutionResult.outcome` を直接採用する。分類は既存の `ExecutionResult`
が担保済み ([src/trading/broker_adapter.py](../../../src/trading/broker_adapter.py))。

- `skipped` — 既存ポジション / scale-in 無効 / risk gate。運用上「無害・想定内」のもののみ。
- `rejected` / `failed` — MT5 拒否 / bridge 異常。**必ずこちらに分類し、`skipped` に落とさない。**
- `hold` — シグナルが hold。
- `executed` — 発注成功。

MT5 拒否や bridge 異常を `skipped` (無害表示) に誤分類しないことが本設計の安全要件であり、
テスト名にもこの規律を明示する (例: `test_mt5_rejection_classified_rejected_not_skipped`)。

### 例外: ATR レイヤが buy/sell を hold へ降格したルート

ATR SL/TP 処理には buy/sell を hold へ降格する経路が2つある:

- `_apply_atr_sltp_to_signal` の R:R 不足判定 — `sig.action="hold"` /
  `sig.signal_reason="ATR R:R too low (...)"` へ書き換える。
- `_execute_one_signal` の SL/TP 算出失敗判定 (`sltp_result is None`) — `sig.action="hold"` /
  `sig.signal_reason="ATR SL/TP calculation failed"` へ書き換える。

いずれも書き換え後に broker は hold を `ExecutionResult.skipped("hold (発注対象外)")` として
返すため、`status` を `ExecutionResult.outcome` からそのまま採ると
`status="skipped"` / `reason="hold (発注対象外)"` となり、**真の原因 (ATR 降格) が見えなくなる**。

そこで `_execute_one_signal` は次の特例で `SignalOutcome` を構築する:

- 関数入口で元の `sig.action` をローカル変数へ退避する。
- ATR 適用後に「元 action が buy/sell かつ現 action が hold」なら ATR 降格とみなし、
  broker 戻り値の reason を使わず
  `SignalOutcome(status="skipped", action=元の buy/sell, reason=sig.signal_reason)` を構築する
  (`sig.signal_reason` は ATR レイヤが設定した具体的理由 — 上記2経路のいずれか)。

これにより、ATR 降格はサマリー上
`⏭ USDJPY=X BUY SKIPPED — reason: ATR SL/TP calculation failed` (または `ATR R:R too low ...`)
として正しい原因とともに表示される。

## データフロー / 変更箇所

| 関数 / ファイル | 変更内容 |
|---|---|
| `_process_pair` | 成功時の戻り値を `(signal, macro_ctx)` から `PairAnalysisOutcome` に変更。`analysis_store.aggregate` が None (即時 Ollama fallback) のとき `tech_fallback=True`。エラー時は bare `Exception` ではなく `PairAnalysisError(pair, error)` を返す |
| `_process_pair` | `combine_signals` 後に `signal.tv_recommendation` を設定 |
| `bounded` (`_phase_analyze_pairs` 内) | `_process_pair` から例外がエスケープした場合 `PairAnalysisError(pair_cfg.symbol, e)` へ包んで返す |
| `_phase_analyze_pairs` | 結果を `PairAnalysisOutcome` / `PairAnalysisError` に分類。戻り値に `data_health: list[str]` を追加 (`PairAnalysisError.pair` → `"{pair} 分析失敗"`、`tech_fallback` ペア → `"{pair} スナップショット未取得(即時分析fallback)"`) |
| `_adjust_signal_with_rag` | 戻り値を `None` から `str` (補正注記。補正なしなら `""`) に変更。`action` または `combined_score` を変えたとき注記文字列を返す |
| `_execute_one_signal` | 戻り値を `Order \| None` から `SignalOutcome` に変更。入口で元の `sig.action` を退避。ATR 降格 (元 buy/sell → 現 hold) の経路は `SignalOutcome(status="skipped", action=元の buy/sell, reason=sig.signal_reason)` を構築 (broker 戻り値の reason を使わない)。`notify_order_opened` / `notify_signal_skipped` の即時呼び出しを `if not notify_on_cycle_summary:` ゲート内へ移動 |
| `_phase_execute_signals` | 戻り値を `(executed_orders, outcomes: list[SignalOutcome])` に変更。hold 分岐も `SignalOutcome(status="hold")` を積む。`_adjust_signal_with_rag` の注記を受け取り `SignalOutcome.rag_note` へ渡す。`outcome.order` があれば `executed_orders` に追加 |
| `trading_cycle` | Phase 4b 後に `if notify_on_cycle_summary:` で `notify_cycle_summary(CycleSummaryEvent(...))` を1回呼ぶ。halt 分岐の early-return 前にも `halted=True` の `CycleSummaryEvent` を1回送る |
| `notifier.py` | `SignalOutcome` / `CycleSummaryEvent` / `NotifierAdapter.notify_cycle_summary()` を追加 |
| `signal_combiner.py` | `TradeSignal` に `tv_recommendation: str = ""` を追加 |
| `config/schema.py` | `NotifierConfig` に `notify_on_cycle_summary: bool = True` を追加 |
| `config/settings.yaml.example` | `notifier` セクションに `notify_on_cycle_summary: true` をコメント付きで追加 (実 config `config/settings.yaml` はキー省略時に既定 `True` で動くため任意追記) |

## エラー処理 / エッジケース

- **ペア分析エラー** — `_process_pair` が `PairAnalysisError(pair, error)` を返したペアは
  `signals` から除外され、`data_health` に `"{pair} 分析失敗"` を積む。Data 行に表示。
- **tech fallback** — 蓄積スナップショットがなく即時分析へ落ちたペアは `data_health` に
  `"{pair} スナップショット未取得(即時分析fallback)"` を積む。Data 行に表示。
- **halt サイクル** — Phase 4b に到達せず early-return。`halted=True` の `CycleSummaryEvent`
  を送る (outcomes は空)。
- **市場休場** — `is_market_open` が False のサイクルは何も送らない (従来どおり無音)。
- **全ペアエラー** — `outcomes` が空。結果行 `0発注 / 0HOLD / 0拒否 / 0失敗` + Data 行のみの
  メッセージを送る (サイクルが走ったことは可視化する)。
- **通知送信失敗** — `send()` 内の例外は既存どおり warning ログのみで握りつぶす
  (システムを止めない)。

## テスト計画

### `tests/test_cycle_summary.py` (新規)

`notify_cycle_summary` の整形を検証 (notifier を MagicMock し送信文字列をアサート):

- executed ブロック: `score/conf/RR`、`entry/SL/TP`、`drivers:`、`reason:` の各行が含まれる
- hold ブロック: `score/conf`、`drivers:`、`reason:` を含み、`entry/SL/TP` を**含まない**
- rejected ブロック: `reason:` に `ExecutionResult.reason` が出る。
  かつ **「既存ポジション」という文言を含まない** (バグ2 回帰テスト)
- failed ブロック: `❌` 絵文字と `reason:` が出る
- ヘッダーカウント: `N発注 / N HOLD / N拒否 / N失敗`、スキップ件数は >0 のときのみ
- ヘッダー絵文字: 全正常 `🟢` / 拒否失敗あり `⚠️` / halt `🛑`
- halt サマリー: `halted=True` で halt 専用文面
- Data 行: `data_health` 非空で `⚠ Data:` 行が出る / 空なら出ない
- scale-in: `order.is_scale_in=True` でラベルに `(scale-in)`
- TV 省略: `tv_recommendation=""` で `drivers:` 行から `TV` 部分が消える
- RAG 注記: `rag_note` 非空で `reason:` の下に `RAG:` 行が出る / 空なら出ない
- 分類規律: `test_mt5_rejection_classified_rejected_not_skipped` —
  MT5 拒否相当の `SignalOutcome(status="rejected")` が拒否ブロックに出て skipped 扱いされない

### 取引サイクルロジックのテスト (既存 cycle テストへ追加 / 新規)

- `PairAnalysisError`: 分析失敗ペアが `PairAnalysisError(pair, error)` で返り、`_phase_analyze_pairs`
  の `data_health` に正しい pair 名で `"{pair} 分析失敗"` が積まれる
- ATR SL/TP 失敗: `_execute_one_signal` が ATR 失敗時に
  `SignalOutcome(status="skipped", reason="ATR SL/TP calculation failed")` を返し、
  reason が `"hold (発注対象外)"` に上書きされない
- RAG 注記: `_adjust_signal_with_rag` が action/score を変えたとき注記文字列を返し、
  変えないとき `""` を返す。注記が `SignalOutcome.rag_note` に伝播する
- `notify_on_cycle_summary=False` フォールバック時に旧 `notify_order_opened` /
  `notify_signal_skipped` が発火し、`True` 時には発火しないこと

### 既存テストの更新

- `_execute_one_signal` の戻り値が `SignalOutcome` になることに追従
- `_phase_execute_signals` の戻り値が `(executed_orders, outcomes)` になることに追従
- `_process_pair` / `_phase_analyze_pairs` が `PairAnalysisOutcome` / `PairAnalysisError`
  を扱うことに追従

## 将来拡張 (本スコープ外)

- `data_health` を `DataHealthItem` (severity 付き) へ拡張。
- 旧 `notify_order_opened` / `notify_signal_skipped` メソッドと旧 config フラグの削除。
- 集約サマリーの embed 化 (色付きサイドバー)。
