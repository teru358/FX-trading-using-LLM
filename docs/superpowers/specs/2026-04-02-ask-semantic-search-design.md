# askコマンド セマンティック検索強化 設計書

## 概要

askコマンドのデータ検索を時間ベースのフィルタリングからセマンティック検索に切り替え、全データソース（ニュース、方向別RAG、振り返り、取引実績、予測的中率）を横断的に検索して、質問に最も関連するコンテキストをLLMに注入する。

### 目的

- ユーザーの質問内容に意味的に関連する過去データを自動取得する
- 現在askが参照していないデータソース（振り返り、方向別RAG、取引結果、予測的中率）を回答に含める
- 具体的な数値や過去の類似パターンを引用した詳細な回答を可能にする

### 現状の問題

- 質問のベクトル化を行っておらず、セマンティック検索が一切ない
- 時間ベースのフィルタリングのみで、質問の意図に関係ないデータが多く含まれる
- `fx_reflections`、方向別RAG、取引実績、予測的中率がaskの回答対象外

---

## セクション1: セマンティック検索エンジン

### 検索対象コレクション（5つ）

| コレクション | データ内容 | 検索top_k |
|---|---|---|
| `fx_news` | ニュース分析（sentiment, themes） | 5 |
| `fx_reflections` | 既存の振り返り（レガシー） | 3 |
| `fx_reflections_bullish` | bullish方向の取引/予測/HOLD記録 | 3 |
| `fx_reflections_bearish` | bearish方向の取引/予測/HOLD記録 | 3 |
| `trading_sessions` (SQLite) | 構造化された取引結果 | 5 |

### 検索フロー

```
ユーザーの質問
    ↓
1. 質問テキストをベクトル化 (embed_text)
    ↓
2. 通貨ペア抽出 (正規表現: "EUR", "EURUSD", "ドル円" etc.)
    ↓
3. 全ChromaDBコレクションに並列セマンティック検索
   (通貨ペアが特定できればwhere filter {"pair": {"$eq": symbol}} で絞る)
   (ペアが特定できない場合はフィルタなしで全ペア横断検索)
    ↓
4. SQLite trading_sessions はペア + 直近N件で取得
    ↓
5. 全結果をマージ → 類似度スコア順にソート → 上位を選定
    ↓
6. コンテキストとしてプロンプトに注入
```

### 通貨ペア抽出ロジック

- 正規表現で `EURUSD`, `USD/JPY`, `ドル円`, `ユーロドル` 等を検出
- 複数ペアが含まれる場合は全てを対象
- ペアが検出できない場合はフィルタなし（全ペア横断検索）
- 関連ペアの拡張: `EURUSD` → `EURUSD=X` にマッピング（既存のsymbol形式に合わせる）

---

## セクション2: コンテキスト構築と拡充

### 現行のコンテキスト（維持）

- テクニカルスナップショット（最新1件/銘柄）
- 直近ニュース（カテゴリ別）
- オープンポジション

### 新規追加のコンテキスト

**1. セマンティック検索結果（質問に関連するデータ）**

```
=== Related Context (by relevance) ===
[trade] EURUSD bearish win | score=-0.35 | entry=1.150 close=1.130 | pnl=+20.0
[forecast] EURUSD bearish correct | score=-0.40 | predicted=bearish actual=bearish
[news] FX sentiment: ECB rate decision dovish (score=-0.30)
[hold] EURUSD HOLD_CORRECT(wrong_dir) | avoided loss
[reflection] EURUSD: lesson → bearish momentum sustained after ECB
```

各エントリにソース種別タグを付けて、LLMが情報の性質を判別できるようにする。

**2. 取引実績サマリー（SQLite集計）**

質問にペアが含まれる場合、そのペアの実績を構造化して注入:

```
=== Trade History: EURUSD=X ===
Total: 12 trades | Win: 7 (58%) | Loss: 5
Total PnL: +32.50 | Avg PnL: +2.71
Last 5: win, loss, win, win, loss
Best: +20.0 (take_profit) | Worst: -10.0 (stop_loss)
```

ペア指定がない場合は全体の要約を注入する。

**3. 予測的中率サマリー（SQLite集計）**

```
=== Forecast Accuracy (24h) ===
EURUSD=X: 8 forecasts | Correct: 5 (63%) | Significant: 3
USDJPY=X: 6 forecasts | Correct: 4 (67%) | Significant: 2
```

### コンテキスト優先順位

LLMのコンテキスト枠を考慮して、以下の優先順で注入:

1. **オープンポジション**（常に最優先、現在の状態）
2. **セマンティック検索結果**（質問に最も関連するデータ）
3. **テクニカルスナップショット**（現在の市場分析）
4. **取引実績サマリー**（ペアの成績）
5. **予測的中率サマリー**（予測の信頼性）
6. **直近ニュース**（市場環境）

---

## セクション3: プロンプト改善

### ask_system.txt の拡張

```
You are an expert FX swing trader and technical analyst with 20 years of experience.
The user will ask questions or share observations about the FX market.

You have access to the following context data:
- Technical analysis snapshots (current direction, bias score, confidence)
- Semantic search results: related past data (trade outcomes, forecast verifications, hold decisions)
- Trade history summary (win rate, PnL)
- Forecast accuracy
- Recent news with sentiment scores
- Open positions

Response rules:
- When data supports your answer, cite specific numbers
- Reference similar past patterns when available
- When asked about direction, clearly state bullish/bearish rationale
- Distinguish between facts (from data) and your interpretation
- Always respond in Japanese
```

### ask_user.j2 の改善

```jinja
{{ open_positions }}

{{ semantic_results }}

{{ trade_summary }}

{{ forecast_accuracy }}

{{ technical_snapshots }}

{{ news_context }}

=== User's Question / Comment ===
{{ user_message }}

上記のコンテキストをもとに、具体的なデータを引用しながら日本語で回答してください。
過去の類似パターンやトレード実績があれば積極的に参照してください。
```

---

## セクション4: 実装構成

### 新規ファイル

| ファイル | 責務 |
|---|---|
| `src/rag/ask_context_builder.py` | セマンティック検索 + 全コンテキスト構築を一元管理 |

### 修正ファイル

| ファイル | 変更内容 |
|---|---|
| `src/trading_cycle.py` | `_build_ask_context()` と `_run_ask()` を新しいcontext builderに差し替え |
| `prompts/ask_system.txt` | 拡張したシステムプロンプト |
| `prompts/ask_user.j2` | セクション分けしたテンプレート |

### ask_context_builder.py の責務

```python
class AskContextBuilder:
    """askコマンド用のコンテキストを構築する。"""

    async def build(self, user_message: str, config, store, analysis_store,
                    session_store, forecast_store, position_mgr) -> dict:
        """
        1. 質問からペアを抽出
        2. embed_text で質問をベクトル化
        3. 全コレクションに並列セマンティック検索
        4. SQLiteから取引実績・予測的中率を集計
        5. 既存コンテキスト（テクニカル、ニュース、ポジション）も取得
        6. 全結果を辞書で返す（テンプレートの各セクションに対応）
        """
```

### 通貨ペア抽出

```python
_PAIR_PATTERNS = {
    "ドル円": "USDJPY=X", "ユーロドル": "EURUSD=X",
    "USDJPY": "USDJPY=X", "EURUSD": "EURUSD=X",
    "USD/JPY": "USDJPY=X", "EUR/USD": "EURUSD=X",
    # ... 設定のinstrumentsから動的にも生成
}

def extract_pairs(message: str, instruments: list) -> list[str]:
    """質問から通貨ペアシンボルを抽出。見つからなければ空リスト。"""
```

### trading_cycle.py の変更

`_run_ask()` 内の `_build_ask_context()` 呼び出しを `AskContextBuilder.build()` に置き換える。既存の `_build_ask_context()` は削除。`_run_ask()` に `session_store` と `forecast_store` を追加で渡す。
