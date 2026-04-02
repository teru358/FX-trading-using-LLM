# 方向別RAGデータ管理 設計書

## 概要

取引結果・予測サイクル・振り返りデータを「上昇（bullish）」と「下降（bearish）」の2方向に分離し、シグナル結合時に方向別の過去データから多角的なスコア補正を行う仕組みを導入する。

### 目的

- 現在の一括RAG蓄積を方向別に分離し、より精度の高い類似局面検索を実現
- 「似た上昇局面で負けている」→ 下方補正、「下降パターンに一致」→ 反転リスク警告、等の状況分析を可能にする
- 取引セッションIDで発注→クローズの1サイクルを追跡・記録する

## アーキテクチャ: ハイブリッド型

- **ChromaDB**: 方向別コレクション（`fx_reflections_bullish` / `fx_reflections_bearish`）に分析テキストを蓄積。セマンティック検索で類似局面を発見する
- **SQLite**: `trading_sessions` テーブルに構造化データ（価格、PnL、勝敗等）を管理。数値的な集計・勝率算出に使用する
- 2つのストアを `session_id` で紐付ける

---

## セクション1: セッション管理とデータ構造

### SQLite `trading_sessions` テーブル

`prices.db` に新テーブルを追加する。

```sql
CREATE TABLE trading_sessions (
    session_id        TEXT PRIMARY KEY,  -- = order_id (UUID)
    pair              TEXT NOT NULL,
    direction         TEXT NOT NULL,     -- "bullish" / "bearish"
    entry_price       REAL NOT NULL,
    stop_loss         REAL,
    take_profit       REAL,
    position_size     REAL,
    signal_score      REAL,             -- combined_score
    signal_confidence REAL,
    macro_context     TEXT,             -- 発注時マクロスナップショット
    analysis_summary  TEXT,             -- 発注時LLM分析テキスト
    opened_at         TEXT NOT NULL,
    closed_at         TEXT,             -- NULL while open
    close_price       REAL,             -- NULL while open
    close_reason      TEXT,             -- take_profit/stop_loss/manual/timeout/etc
    realized_pnl      REAL,             -- NULL while open
    outcome           TEXT,             -- "win" / "loss" / NULL while open
    reflection_text   TEXT,             -- クローズ時LLM振り返り
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
```

マッピングルール:
- `direction`: `buy` → `"bullish"`, `sell` → `"bearish"`
- `outcome`: `realized_pnl > 0` → `"win"`, `realized_pnl <= 0` → `"loss"`

---

## セクション2: ChromaDBコレクション分離

### コレクション構成

| 旧 | 新 |
|---|---|
| `fx_reflections`（全データ混在） | `fx_reflections_bullish`（上昇方向） |
| | `fx_reflections_bearish`（下降方向） |

### ドキュメント蓄積（2段階）

**Phase 1: 発注時（ポジションオープン）**
- ドキュメントID: `{session_id}_entry`
- テキスト: 発注時の分析サマリー（テクニカル + ニュースセンチメント + マクロコンテキスト）
- メタデータ:
  - `session_id`, `pair`, `direction`
  - `signal_score`, `confidence`, `entry_price`
  - `phase: "entry"`, `opened_at`

**Phase 2: 完了時（ポジションクローズ）**
- ドキュメントID: `{session_id}_complete`
- テキスト: Phase 1の分析 + 取引結果 + LLM振り返りを合成した1サイクル完結テキスト
- メタデータ: Phase 1 に加え:
  - `close_price`, `realized_pnl`
  - `outcome: "win"/"loss"`, `close_reason`
  - `phase: "complete"`

### シグナル結合時の検索フロー

例: combined_scoreが `+0.3`（bullish寄り）の場合

1. `fx_reflections_bullish` を検索 → 類似した上昇局面の過去データ取得
   - `outcome: "win"` が多い → スコア維持 or 上方補正
   - `outcome: "loss"` が多い → 下方補正（似た局面で負けている）
2. `fx_reflections_bearish` を検索 → 類似した下降局面の過去データ取得
   - 類似度が高い → 下降反転リスクあり、下方補正
   - 類似度が低い → 下降パターンには該当しない、スコア維持

---

## セクション3: スコア補正ロジック

### 現行のシグナル結合

```
combined_score = news_weight(0.40) × news_score + price_weight(0.60) × price_score
```

### 新しいフロー

```
combined_score (従来通り算出)
    ↓
RAG方向別検索
    ↓
rag_adjustment (補正値算出)
    ↓
adjusted_score = combined_score + rag_adjustment
    ↓
発注判断は adjusted_score で行う
```

### 補正値算出ロジック

combined_scoreがbullish寄り（> 0）の場合:

```python
# 1. bullishコレクションから類似度上位N件を取得
bullish_hits = search(fx_reflections_bullish, query=current_analysis, n=5)

# 2. bearishコレクションから類似度上位N件を取得
bearish_hits = search(fx_reflections_bearish, query=current_analysis, n=5)

# 3. bullish側: 勝率から信頼度を算出
bullish_win_rate = bullish勝ち件数 / bullish有効件数
bullish_factor = (bullish_win_rate - 0.5) × bullish_weight

# 4. bearish側: 類似度が高いほど反転リスク
bearish_similarity = bearishヒットの平均類似度
bearish_factor = -bearish_similarity × bearish_weight

# 5. 合算
rag_adjustment = bullish_factor + bearish_factor
```

bearish寄り（< 0）の場合は対称的に逆転する:
- `fx_reflections_bearish` の勝率から `bearish_factor` を算出
- `fx_reflections_bullish` の類似度から反転リスク（`bullish_factor`、符号反転）を算出
- `rag_adjustment = bearish_factor + bullish_factor`（結果は負方向に補正）

### 補正の制約

- `rag_adjustment` の範囲: **±0.15** にクランプ（過剰補正防止）
- 有効ヒット数が **2件未満**: 補正なし（データ不足）
- `phase: "complete"` のドキュメントのみ補正に使用（未完了セッション除外）

### 設定値（settings.yaml に追加）

```yaml
rag_adjustment:
  enabled: true
  max_adjustment: 0.15
  min_hits: 2
  search_top_n: 5
  bullish_weight: 0.10
  bearish_weight: 0.10
```

### ログ出力

```
RAG Adjustment: combined=+0.300 → adjusted=+0.255
  bullish: 4 hits, win_rate=0.50, factor=-0.000
  bearish: 3 hits, avg_sim=0.45, factor=-0.045
```

---

## セクション4: 取引ライフサイクル統合

### 発注時（ポジションオープン）

1. シグナル結合 → RAGスコア補正 → adjusted_scoreが閾値超過
2. ペーパーブローカーで発注
3. `trading_sessions` テーブルに INSERT（session_id, pair, direction, entry_price, signal_score, signal_confidence, macro_context, analysis_summary, opened_at）
4. 方向別ChromaDBコレクションに `{session_id}_entry` を注入
5. `positions.json` に `session_id` フィールドを追加して書き込み

### ポジション保持中

- `positions.json` の各ポジションに `session_id` が紐付き
- 既存のレイヤー1〜4リスク管理はそのまま動作

### クローズ時（利確・損切り・タイムアウト等）

1. クローズ条件成立 → ペーパーブローカーでクローズ（既存処理）
2. `session_id` で `trading_sessions` を UPDATE（closed_at, close_price, close_reason, realized_pnl, outcome）
3. `session_id` で ChromaDB の `{session_id}_entry` を検索 → 発注時分析テキスト取得
4. LLMに振り返り生成を依頼（入力: 発注時分析 + 取引結果 + マクロコンテキスト）
5. `trading_sessions` の `reflection_text` を UPDATE
6. ChromaDB に `{session_id}_complete` を注入（発注時分析 + 結果 + 振り返りの合成テキスト）
7. `trades.json` に `session_id` を含めて書き込み（既存処理の拡張）

### positions.json の変更

```json
{
  "account_balance": 10790.41,
  "last_updated": "2026-04-02T11:04:30",
  "open_positions": [
    {
      "order_id": "xxx-xxx",
      "session_id": "xxx-xxx",
      "pair": "EURUSD=X",
      "direction": "buy",
      "..."
    }
  ]
}
```

`session_id` = `order_id` だが、将来的に1セッションに複数注文（分割エントリ等）を対応する場合の拡張余地として明示的に分離する。

---

## セクション5: 既存データ移行

### 移行対象

- `trades.json` の16件のクローズ済み取引
- 既存 `fx_reflections` コレクションのデータ

### 移行スクリプト

配置先: `scripts/migrate_directional_rag.py`（冪等性を持たせる）

**Step 1: trades.json → trading_sessions テーブル**

| trades.json | trading_sessions |
|---|---|
| `order_id` | `session_id` |
| `direction: "buy"` | `direction: "bullish"` |
| `direction: "sell"` | `direction: "bearish"` |
| `signal_reason` をパース | `signal_score`, `signal_confidence` |
| `macro_context_at_entry` | `macro_context` |
| `realized_pnl > 0` | `outcome: "win"` |
| `realized_pnl <= 0` | `outcome: "loss"` |

- `analysis_summary`: `signal_reason` + `macro_context_at_entry` から簡易テキスト生成
- `reflection_text`: 既存データにないため NULL

**Step 2: 方向別ChromaDBコレクション作成**

各取引から `{session_id}_complete` ドキュメントを生成:

```
"{pair} {direction} | score={score} conf={conf} |
 entry={entry_price} close={close_price} |
 result={outcome} pnl={realized_pnl} |
 {macro_context要約}"
```

- bullish 4件 → `fx_reflections_bullish`
- bearish 12件 → `fx_reflections_bearish`

**Step 3: 既存 fx_reflections の処理**

- 方向が特定できるもの → 該当コレクションにコピー
- 方向不明のもの → テキスト内容からbullish/bearish判定を試み、判定不能はスキップ
- 移行完了後、旧 `fx_reflections` は `fx_reflections_legacy` にリネーム保持

**Step 4: 検証**

- `trading_sessions` のレコード数 = 16
- 各コレクションのドキュメント数を確認
- 1件サンプルで検索テストを実施

---

## セクション6: 予測サイクルへの方向別RAG統合

### 現行フロー

```
予測生成 → 24h後に検証 → fx_reflections に日次サマリー蓄積
（方向区別なし、1日1エントリで上書き）
```

### 新フロー

```
予測生成 → 方向別コレクションに entry 注入
    ↓
24h後に検証
    ↓
方向別の的中率を個別算出
    ↓
bullish予測 → fx_reflections_bullish に complete 注入
bearish予測 → fx_reflections_bearish に complete 注入
```

### 変更点

**1. 予測生成時（forecast_cycle Phase 2）**

`|combined_score| ≥ 0.25` の予測が生成された時点で:
- エントリID: `forecast_{pair}_{forecast_id}_entry`
- 格納先: `predicted_direction` が bullish → `fx_reflections_bullish`、bearish → `fx_reflections_bearish`
- メタデータ: `session_type: "forecast"`, `phase: "entry"`, `signal_score`, `confidence`, `pair`

**2. 検証時（forecast_cycle Phase 1）**

`build_forecast_review_summary()` の出力を方向別に分離:
- bullish予測の的中/外れを集計 → `fx_reflections_bullish` に complete 蓄積
- bearish予測の的中/外れを集計 → `fx_reflections_bearish` に complete 蓄積
- エントリID: `forecast_{pair}_{forecast_id}_complete`
- メタデータに `outcome: "correct"/"incorrect"` を追加

**3. HOLD判断レビューも同様**

`_review_hold_decisions()` の結果:
- `predicted_direction` に基づいて方向別コレクションに振り分け
- `SHOULD_HAVE_ENTERED` → その方向のデータとして蓄積（見逃した機会）
- `HOLD_CORRECT(wrong_dir)` → 逆方向のデータとして蓄積（回避成功）

**4. スコア補正への影響**

セクション3の補正ロジックに、予測サイクルのデータも自然に組み込まれる:
- 予測と取引のデータが同じコレクションに入るため、類似局面検索に両方が含まれる
- メタデータの `session_type: "trade"/"forecast"/"hold"` で区別可能
- 取引実績の方が予測より信頼度が高いので、補正時に重み付けを分ける

### settings.yaml 追加

```yaml
rag_adjustment:
  trade_weight_multiplier: 1.0      # 取引実績の重み（基準）
  forecast_weight_multiplier: 0.5   # 予測検証の重み（取引の半分）
  hold_weight_multiplier: 0.3       # HOLD判断の重み
```

### significanceフィルタとの関係

既存の ATR_proxy × 0.30 フィルタはそのまま維持する。significantでない予測は引き続きスキップされ、方向別コレクションにも蓄積されない。
