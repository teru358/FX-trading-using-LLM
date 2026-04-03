# ATRベースSL/TP算出 + LLM適応パラメータ 設計書

## 概要

LLMに全面依存していたSL/TP決定をATRベースのコード算出に切り替え、SL距離が極端に狭くなる問題を解消する。LLMの提案値は記録・比較し、取引クローズ時の振り返りでLLMがATR倍率の改善を提案する学習ループを構築する。

### 目的

- SLが0.2〜5pipsという異常に狭い値になる問題を根本的に解消する
- ATR(14)に基づく合理的なSL/TP距離を保証する
- LLMの出力と計算値を比較記録し、振り返りで最適化していく
- ペア別の動的パラメータをYAMLファイルで管理し、将来の自律改善プロセスの基盤とする

### 現状の問題

- 18トレード中11件（61%）がSLヒットでクローズ
- EURUSD でSL距離が0.2〜5pips（スイングトレードではノイズレベル）
- RR=224のような異常値（SLがentryとほぼ同値）
- LLM（ローカル12Bモデル）の価格値生成精度が不十分

---

## セクション1: ATRベースSL/TP算出

### 新しいSL/TP決定フロー

```
LLM出力:
  direction_bias, bias_score, confidence,
  stop_loss, take_profit (参考値として維持),
  key_support, key_resistance (新規)
    ↓
ATR算出 (IndicatorSummary.atr_value = ATR(14))
    ↓
コード側でSL/TP算出:
  LONG:  SL = entry - ATR × sl_atr_mult
         TP = entry + ATR × tp_atr_mult
  SHORT: SL = entry + ATR × sl_atr_mult
         TP = entry - ATR × tp_atr_mult
    ↓
スイングH/L・key_support/resistance で微調整:
  - SLが直近サポート/レジスタンスの外側にあれば寄せる
  - ただしATR×min倍率を下回らないよう制限
    ↓
LLM出力値との比較・記録:
  computed_sl, computed_tp, llm_sl, llm_tp, adopted, atr_value, 倍率
    ↓
計算値を優先して採用
```

### LLM出力スキーマの変更

既存の JSON に `key_support` と `key_resistance` を追加:

```json
{
  "direction_bias": "long|short|neutral",
  "bias_score": -1.0 to 1.0,
  "confidence": 0.0 to 1.0,
  "entry_zone": [low, high],
  "stop_loss": price,
  "take_profit": price,
  "risk_reward_ratio": float,
  "key_support": price,
  "key_resistance": price,
  "reasoning_summary": "日本語1文"
}
```

`key_support` / `key_resistance` は LLM がスイングH/L や一目の雲などから意識する価格帯。コード側のSL/TP微調整に使用する。

### ATR倍率の設定

```yaml
# settings.yaml（デフォルト・上下限）
trading:
  sl_atr_mult_default: 1.5
  tp_atr_mult_default: 3.0
  sl_atr_mult_min: 0.5
  sl_atr_mult_max: 3.0
  tp_atr_mult_min: 1.0
  tp_atr_mult_max: 6.0
```

ペア別の現在値は `adaptive_params.yaml` から取得。未登録の場合は settings.yaml のデフォルト値を使用。

---

## セクション2: 適応パラメータストア

### `data/state/adaptive_params.yaml`

```yaml
# LLMが取引振り返り時に自動更新するペア別パラメータ
# 手動編集可。次回取引サイクルで即座に反映される。
_schema_version: 1

defaults:
  sl_atr_mult: 1.5
  tp_atr_mult: 3.0

pairs:
  EURUSD=X:
    sl_atr_mult: 1.5
    tp_atr_mult: 3.0
    updated_at: "2026-04-03T10:00:00"
    reason: "初期値"
    history:
      - sl_atr_mult: 1.5
        tp_atr_mult: 3.0
        updated_at: "2026-04-03T10:00:00"
        reason: "初期値"
        trade_id: null
```

### 設計方針

- `defaults` は settings.yaml から初回ロード時にコピー
- `pairs` はペア別の現在値 + 変更履歴（`history`、直近10件保持）
- 変更履歴に `trade_id`（どの取引が契機か）と `reason`（LLMの判断理由）を記録
- 既存の `StateStore._atomic_write()` パターンを再利用（tmp→rename、.bak自動保存）

### AdaptiveParamsStore クラス

```python
class AdaptiveParamsStore:
    """LLMが更新するペア別動的パラメータの管理。"""

    def get_params(self, pair: str) -> dict:
        """ペアの現在パラメータを返す。未登録ならdefaults。"""

    def update_params(self, pair: str, new_params: dict,
                      reason: str, trade_id: str | None) -> None:
        """パラメータを更新し履歴に追記。クランプ適用。"""

    def get_history(self, pair: str, limit: int = 3) -> list[dict]:
        """変更履歴を返す（振り返りプロンプトに注入用）。"""
```

### 将来の拡張性

今回は `sl_atr_mult` と `tp_atr_mult` のみ。将来の自律改善候補:

- `news_weight` / `price_weight` — ニュース/テクニカルの最適比率
- `signal_deadband` — シグナル閾値の最適化
- `max_holding_days` — 最適保有期間
- `trailing_stop_activation_pct` — トレーリングストップの最適発動タイミング

同じ `pairs` 辞書にキーを足すだけで拡張できる。

---

## セクション3: 振り返り時のLLM学習フィードバック

### 新フロー

```
クローズ → generate_close_reflection()
  入力に追加:
    - 採用したSL/TP (computed)
    - LLMが提案したSL/TP
    - ATR値、使用した倍率
    - 実際の価格到達点（close_price）
    - パラメータ変更履歴（直近3件）
  ↓
  LLM出力JSONに追加:
    "atr_params_suggestion": {
      "sl_atr_mult": 1.8,
      "tp_atr_mult": 3.0,
      "reason": "SLが短時間で刈られた。ボラティリティに対して狭い"
    }
  ↓
  バリデーション:
    - min/max クランプ (settings.yaml の上下限)
    - 前回値からの変動幅制限（1回の変更で ±0.5 以内）
  ↓
  adaptive_params.yaml を更新
  ↓
  変更理由をRAGに蓄積（方向別コレクション）
```

### 振り返りプロンプトへの追加入力

既存の `_CLOSE_REFLECTION_PROMPT` に以下のセクションを追加:

```
=== SL/TP Analysis ===
ATR(14) at entry: {atr_value:.5f}
Computed SL: {computed_sl:.5f} (ATR × {sl_atr_mult})
Computed TP: {computed_tp:.5f} (ATR × {tp_atr_mult})
LLM suggested SL: {llm_sl:.5f}
LLM suggested TP: {llm_tp:.5f}
Adopted: {adopted}
Actual close: {close_price:.5f} ({close_reason})

=== Parameter History (last 3) ===
{param_history}

=== Additional Task ===
Based on the trade outcome vs the SL/TP setup:
- Was the SL distance appropriate? Too tight (hit by noise)? Too wide (excessive loss)?
- Was the TP distance appropriate? Too ambitious (never reached)? Too conservative?
- Should the ATR multipliers be adjusted for this pair?

If adjustment is needed, include in your JSON:
"atr_params_suggestion": {
  "sl_atr_mult": <new_value or null if no change>,
  "tp_atr_mult": <new_value or null if no change>,
  "reason": "<why this change>"
}
If no adjustment needed, set "atr_params_suggestion": null
```

### 変動幅制限

- 1回の更新で倍率の変更幅は **±0.5** 以内
- settings.yaml の `sl_atr_mult_min`(0.5) 〜 `sl_atr_mult_max`(3.0) でクランプ
- 変更が却下された場合もログに記録

### 比較記録の蓄積

RAG completeドキュメントのテキストにSL/TP比較情報を含める:

```
EURUSD bearish | ... | sl_comparison: computed=1.1450(ATR×1.5) llm=1.1495 adopted=computed | ...
```

方向別RAG検索で過去のSL/TP判断パターンも参照可能になる。

---

## セクション5: 発注時の保存データ拡充

### 現状の問題

TradeSignal は `news: NewsSentiment` と `price: PriceAnalysis` の完全なオブジェクトを持っているが、Order に渡す時点で `signal_reason`（`"score=-0.373 conf=0.75"` のみ）と `macro_context_at_entry` に圧縮される。振り返り時にニュース内容やテクニカル詳細が失われている。

### 発注時に追加保存するデータ

`trading_sessions` テーブルの `analysis_summary` カラム（既存、現在は `detail_reason` を格納）を拡充し、取引に至った理由を網羅的に保存する:

```
=== Signal Summary ===
combined_score=-0.373 confidence=0.75 action=sell
news_weight=0.40 price_weight=0.60

=== News Sentiment ===
score=-0.25 confidence=0.70
key_themes: ECB rate decision, eurozone inflation
bullish_factors: strong employment data
bearish_factors: ECB dovish guidance, weak PMI
summary: ECBの利下げ示唆でユーロ売り圧力

=== Technical Analysis ===
direction=short bias_score=-0.50 confidence=0.80
reasoning: SMA20<SMA50、MACD弱気クロス、RSI=42
entry_zone=[1.1530, 1.1540]
key_support=1.1480 key_resistance=1.1560

=== SL/TP Decision ===
ATR(14)=0.0043 sl_atr_mult=1.5 tp_atr_mult=3.0
computed: SL=1.1600 TP=1.1406
llm: SL=1.1535 TP=1.1490
adopted=computed

=== Macro Context ===
(既存のmacro_context_at_entry)
```

### 方向別RAG entry ドキュメントにも同内容を含める

`{order_id}_entry` のテキストにニュースと テクニカルのサマリーを含めることで、将来の類似局面検索でファンダ+テクニカルの両面が参照可能になる。

### 振り返りプロンプトへの注入

精算時の `generate_close_reflection()` に上記の保存データを全文注入する。これにより振り返りLLMは以下を全て評価できる:

- **ニュース判断**: ファンダメンタルの読みは正しかったか
- **テクニカル判断**: 方向性・エントリーゾーンは適切だったか
- **SL/TP判断**: ATR倍率は適切だったか
- **マクロ判断**: 監視銘柄の示唆は正しかったか
- **総合判断**: news_weight/price_weight のバランスは適切だったか

振り返り結果は既存の方向別RAG（bullish/bearish）に蓄積される。

---

## セクション6: 実装構成

### 新規ファイル

| ファイル | 責務 |
|---|---|
| `src/trading/atr_calculator.py` | ATRベースのSL/TP算出、スイングH/Lとの調整、LLM出力との比較記録生成 |
| `src/persistence/adaptive_params_store.py` | `adaptive_params.yaml` の読み書き、クランプ、変更履歴管理 |
| `src/trading/entry_context_builder.py` | 発注時のコンテキスト（ニュース+テクニカル+SL/TP+マクロ）をテキスト化して保存用に構築 |

### 修正ファイル

| ファイル | 変更内容 |
|---|---|
| `src/config.py` | `TradingConfig` にATR倍率のデフォルト・上下限を追加 |
| `config/settings.yaml` | ATR倍率のデフォルト・上下限設定を追加 |
| `src/signals/signal_combiner.py` | LLMのSL/TPの代わりにATR算出値を使用 |
| `src/analysis/price_analyzer.py` | LLM出力に `key_support`, `key_resistance` を追加パース |
| `prompts/price_user.j2` | LLMに `key_support`, `key_resistance` の出力を要求 |
| `src/analysis/reflector.py` | 振り返りプロンプトにSL/TP比較+ニュース+テクニカル全文を追加、`atr_params_suggestion` をパース |
| `src/trading_cycle.py` | Phase 4b: ATR算出+コンテキスト保存、クローズ振り返りでパラメータ更新 |

### データフロー全体像

```
[取引発注時]
  TradeSignal (news + price + combined_score)
    + IndicatorSummary.atr_value
    + adaptive_params.yaml のペア別倍率
    + LLMの key_support / key_resistance
    → atr_calculator.calculate_sl_tp() → SLTPResult
    → entry_context_builder.build() → 網羅的コンテキストテキスト
    → session_store に analysis_summary として保存
    → 方向別RAG に entry ドキュメントとして蓄積

[取引クローズ時]
  session_store から analysis_summary を取得
    → 振り返りLLM に全文注入
      (ニュース + テクニカル + SL/TP比較 + マクロ + 変更履歴)
    → 振り返り結果 + atr_params_suggestion をパース
    → adaptive_params_store.update_params()
    → 方向別RAG に complete ドキュメントとして蓄積
```
