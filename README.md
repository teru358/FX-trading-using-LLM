# FX Paper Trading System

ニュース RAG × LLM を組み合わせた FX スウィングトレード自動売買システム（ペーパートレード）。

---

## 概要

| 項目 | 内容 |
|---|---|
| 取引モード | ペーパートレード（模擬） / OANDA本取引（スタブ） |
| 取引スタイル | スウィング（数日〜数週間） |
| 価格データ | yfinance（Yahoo Finance・無料） |
| ニュース取得 | RSS（FX専門・世界情勢・日本情勢） |
| 分析エンジン | LLM（Ollama / Gemini / OpenAI / Claude — 分析種別ごとに個別設定可） |
| ベクトル化 | Ollama `nomic-embed-text`（ローカル） |
| RAGストア | ChromaDB（ローカルファイルDB） |
| 言語 | Python 3.12 |
| パッケージ管理 | uv |

デフォルト構成では外部APIキー不要。Ollama のみですべてローカル動作します。

---

## アーキテクチャ

### 情報収集ループ（15分間隔・タイムゾーン基準の固定時刻）

1. RSS フィード取得（FX専門・世界情勢・日本情勢）
   - LLM でセンチメント分析
   - nomic-embed-text でベクトル化 → ChromaDB に蓄積
2. yfinance で OHLCV 取得 → SQLite にキャッシュ（差分取得）
3. テクニカル指標計算（SMA/EMA/RSI/MACD/BB/ATR/ADX/一目均衡表）
4. LLM でテクニカル分析
   - SQLite にスナップショットとして蓄積（48時間で自動削除）

### 取引判定ループ（15:00 / 21:30 JST / 土日スキップ）

1. **Phase 1**: 既存ポジションの SL/TP 到達確認・クローズ
2. **Phase 2**: オープンポジションの振り返り生成 → ChromaDB 蓄積
3. **Phase 3**: テクニカルスナップショットを時間加重集約（直近8h）
   - スナップショット未蓄積時は LLM 即時分析にフォールバック
4. **Phase 4**: RAG からニュースセンチメントを集約
   - シグナル統合（テクニカル60% + ニュース40%）
   - BUY/SELL/HOLD 判定（HOLD時も方向予測を表示）
5. **Phase 5**: ペーパー注文執行・レポート出力

---

## ディレクトリ構成

```
finance/
├── main.py                         # エントリーポイント・スケジューラ
├── pyproject.toml
├── config/
│   ├── settings.yaml               # 全設定
│   ├── settings.yaml.example       # 設定テンプレート（基本構成）
│   └── user_notes.md               # ユーザーの裁量判断メモ（価格分析プロンプトに注入）
├── src/
│   ├── config.py                   # 設定ローダー・データクラス
│   ├── logging_setup.py            # ログ設定（メイン・アクティビティ・ターミナル）
│   ├── startup.py                  # 起動時チェック（Ollama・ディレクトリ）
│   ├── trading_cycle.py            # 取引サイクル（5フェーズ オーケストレータ）
│   ├── llm/
│   │   ├── client.py               # LLMClient ABC（全プロバイダー共通インターフェース）
│   │   ├── factory.py              # ロール別ファクトリ（news/price/reflection → 各クライアント）
│   │   ├── response_parser.py      # LLMレスポンスJSON抽出（<think>タグ除去対応）
│   │   ├── ollama_client.py        # Ollama HTTP クライアント
│   │   ├── gemini_client.py        # Google Gemini API クライアント
│   │   ├── openai_client.py        # OpenAI API クライアント
│   │   └── claude_client.py        # Anthropic Claude API クライアント
│   ├── analysis/
│   │   ├── news_analyzer.py        # RSS → LLM ニュースセンチメント分析
│   │   ├── news_aggregator.py      # RAG ニュース集約（取引判定時）
│   │   ├── rss_fetcher.py          # RSS フィード取得・フィルタリング
│   │   ├── price_analyzer.py       # テクニカル指標 → LLM 価格分析
│   │   └── reflector.py            # 振り返り生成・RAG蓄積
│   ├── data/
│   │   ├── price_fetcher.py        # yfinance OHLCV取得（差分フェッチ対応）
│   │   ├── price_store.py          # SQLite OHLCVキャッシュ
│   │   ├── analysis_store.py       # SQLite テクニカルスナップショット（48h自動削除）
│   │   ├── indicators.py           # テクニカル指標（pandas-ta + 一目均衡表）
│   │   └── indicator_formatter.py  # LLMプロンプト用フォーマッタ
│   ├── jobs/
│   │   ├── news_collector.py       # RSSニュース収集・センチメント分析・RAG格納
│   │   ├── technical_collector.py  # OHLCV取得・テクニカル分析・スナップショット保存
│   │   └── price_monitor.py        # オープンポジション価格監視・急変動通知・緊急損切り
│   ├── signals/
│   │   └── signal_combiner.py      # テクニカル×ニュース シグナル統合
│   ├── trading/
│   │   ├── broker_adapter.py       # BrokerAdapter ABC
│   │   ├── paper_broker.py         # ペーパートレード実装
│   │   ├── paper_trader.py         # 模擬注文・SL/TP判定
│   │   ├── live_broker.py          # OANDA本取引（スタブ）+ ファクトリ
│   │   ├── market_hours.py         # FX市場開閉判定（NY時間基準・DST自動対応）
│   │   └── position_manager.py     # ポジション・残高・PnL管理
│   ├── rag/
│   │   ├── vector_store.py         # ChromaDB ラッパー（ニュース・振り返り）
│   │   ├── embedder.py             # nomic-embed-text ベクトル化
│   │   └── prompt_formatter.py     # RAGデータのプロンプト整形
│   ├── notifications/
│   │   ├── notifier.py             # 通知アダプター（Discord/Telegram/None）
│   │   ├── discord_notifier.py     # Discord Webhook
│   │   └── telegram_notifier.py    # Telegram Bot API
│   ├── persistence/
│   │   └── state_store.py          # JSON アトミック書き込み
│   └── reporting/
│       └── reporter.py             # Rich テーブル表示・レポート
├── data/
│   ├── prices.db                   # SQLite（OHLCVキャッシュ + テクニカルスナップショット）
│   ├── state/                      # ポジション・取引履歴（JSON）
│   └── rag/                        # ChromaDB ファイル
└── logs/
    ├── finance.log                 # 全ログ（DEBUG以上・ローテーション対応）
    └── activity.log                # 取引・ニュース活動ログ
```

---

## セットアップ

### 前提

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/)（デフォルト構成の場合）

### インストール

```bash
# Ollama モデルの準備（デフォルト構成）
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# 依存パッケージのインストール
uv sync

# オンラインLLMを使う場合は .env を作成
cp .env.example .env
# .env に GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY を設定
```

### 実行

```bash
uv run python main.py
```

起動直後にニュース収集を1回実行し、その後スケジュールに従って動作します。

---

## LLM プロバイダー設定

3種類の分析（ニュース分析・価格分析・振り返り生成）それぞれに異なるLLMプロバイダーを設定できます。

### 対応プロバイダー

| プロバイダー | 設定値 | 必要な環境変数 | デフォルトモデル |
|---|---|---|---|
| Ollama（ローカル） | `"ollama"` | なし | `llama3.1:8b` |
| Google Gemini | `"gemini"` | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| OpenAI (ChatGPT) | `"openai"` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| Anthropic Claude | `"claude"` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` |

### 設定例

```yaml
# config/settings.yaml

llm:
  ollama:
    base_url: "http://localhost:11434"
    timeout_seconds: 120
    max_retries: 2
    max_concurrent: 2              # 同時分析数上限（VRAM保護）

  # 各ロールに provider と model を個別指定可能
  news_analysis:
    provider: "ollama"
    temperature: 0.3
    model: "phi4:14b"              # ロール別にモデルを指定
  price_analysis:
    provider: "ollama"
    temperature: 0.1
    model: "mistral-nemo:12b"
  reflection:
    provider: "ollama"
    temperature: 0.3
    model: "deepseek-r1:14b"
```

デフォルト構成ではすべて `provider: "ollama"`（完全ローカル動作・APIキー不要）。

`model:` を省略すると各プロバイダーのデフォルトモデルが使用されます。Ollama の場合は `llama3.1:8b`（`src/config.py` の `_DEFAULT_OLLAMA_MODEL` で定義）。

プロバイダーをロールごとに混在させることも可能です:

```yaml
  news_analysis:
    provider: "gemini"             # ニュース分析は Gemini
    temperature: 0.3
  price_analysis:
    provider: "ollama"             # 価格分析はローカル
    temperature: 0.1
    model: "mistral-nemo:12b"
  reflection:
    provider: "claude"             # 振り返りは Claude
    temperature: 0.3
```

### プロバイダー別の詳細設定

```yaml
# Ollama 接続設定（llm.ollama セクション）
llm:
  ollama:
    base_url: "http://localhost:11434"
    timeout_seconds: 120
    max_retries: 2
    max_concurrent: 2

gemini:
  model: "gemini-2.0-flash"
  timeout_seconds: 60
  max_retries: 2

openai:
  model: "gpt-4o-mini"
  timeout_seconds: 60
  max_retries: 2

claude:
  model: "claude-haiku-4-5-20251001"
  timeout_seconds: 60
  max_retries: 2
  max_tokens: 4096
```

---

## 設定リファレンス（`config/settings.yaml`）

### 取引設定

```yaml
trading:
  initial_balance: 10000.0          # ペーパー口座の初期残高（USD）
  risk_per_trade: 0.02              # 1トレードあたりのリスク（残高の2%）
  signal_confidence_threshold: 0.55 # シグナル発動の最低信頼度
  max_concurrent_positions: 3       # 最大同時ポジション数
  lookback_days: 90                 # 価格データの取得期間（日）
  ohlcv_interval: "1h"              # yfinance の足種
  news_weight: 0.40                 # シグナル統合のニュース比重
  price_weight: 0.60                # シグナル統合のテクニカル比重（合計1.0必須）
  signal_deadband: 0.15             # BUY/SELL判定のスコア閾値（±）
  min_lot_size: 1000.0              # 最小ロットサイズ
  lot_unit: 1000.0                  # ロット切り捨て単位
  trading_mode: "paper"             # "paper"（模擬）| "live"（OANDA本取引）
```

### 通貨ペア

```yaml
pairs:
  - symbol: "USDJPY=X"              # yfinance シンボル
    display_name: "USD/JPY"
    pip_value: 0.01
    base_currency: "USD"
    quote_currency: "JPY"
    enabled: true                    # false で無効化
```

### スケジュール

```yaml
news_collection:
  interval_minutes: 15               # 収集間隔
  timezone: "Asia/Tokyo"
  inter_pair_delay_seconds: 10       # ペア間の待機（VRAM保護）

schedule:
  run_times:
    - "15:00"
    - "21:30"
  timezone: "Asia/Tokyo"
```

収集ジョブは `interval_minutes` と `timezone` から固定時刻を自動生成します（例: 15分間隔 → 00:00, 00:15, 00:30 ... 23:45）。毎時:00にはテクニカル分析も実行し、:15/:30/:45はニュースのみ。

### RAG設定

```yaml
rag:
  db_path: "data/rag"
  embedding_model: "nomic-embed-text"
  news_lookback_hours: 24            # 取引判定時に参照するニュースの期間
  retrieval_top_k: 5
  reflection_lookback_count: 3       # 参照する振り返りの件数
  analysis_lookback_hours: 8         # テクニカルスナップショットの集約時間幅
```

### 通知

```yaml
notification:
  notifier: "none"                   # "telegram" | "discord" | "none"
  notify_on_order_open: true
  notify_on_order_close: true
  notify_on_signal_skipped: false
```

---

## ニュースソース（RSS）

| カテゴリ | ソース |
|---|---|
| FX専門 | ForexLive, FXStreet, Investing.com Forex |
| 世界情勢・株価 | Reuters Business, BBC Business, AP Business, Financial Times, Yahoo Finance, Investing.com Stock News |
| 日本情勢・株価 | NHK World, Japan Today, Japan Times, Nikkei Asia, 日本経済新聞（マーケット） |

JPYを含む通貨ペアでは日本情勢・株価フィードが自動的に追加されます。

---

## シグナルロジック

```
combined_score = テクニカルスコア × price_weight + ニューススコア × news_weight

# ニュースとテクニカルが逆方向の場合はスコア・信頼度を50%減衰
if 方向が相反する:
    combined_score *= 0.5
    confidence *= 0.5

# 判定
score > +signal_deadband  かつ  confidence >= threshold  → BUY  (▲ bullish)
score < -signal_deadband  かつ  confidence >= threshold  → SELL (▼ bearish)
それ以外                                                  → HOLD (方向予測は表示)

# ポジションサイジング
lot = (残高 × risk_per_trade) ÷ ストップ幅(pips)
lot = max(min_lot_size, round(lot / lot_unit) * lot_unit)
```

---

## テクニカル指標

| 指標 | パラメータ | 用途 |
|---|---|---|
| SMA | 20 / 50 / 200日 | トレンド方向 |
| EMA | 12 / 26日 | 短期モメンタム |
| RSI | 14日 | 過熱感 |
| MACD | 12-26-9 | モメンタム転換 |
| Bollinger Bands | 20日・2σ | 価格位置（%B） |
| ATR | 14日 | ボラティリティ |
| ADX | 14日 | トレンド強度 |
| **一目均衡表** | 9 / 26 / 52日 | **トレンド・サポレジ・シグナルの統合判断** |

### 一目均衡表の判定ロジック

| 要素 | 計算 | 判断への活用 |
|---|---|---|
| 転換線 | 9期間高値+安値÷2 | 短期トレンド方向 |
| 基準線 | 26期間高値+安値÷2 | 中期トレンド・サポレジ |
| 先行スパンA | (転換+基準)÷2 を26期間先行 | 雲の上限または下限 |
| 先行スパンB | 52期間高値+安値÷2 を26期間先行 | 雲の上限または下限 |
| 遅行スパン | 現在値を26期間過去に表示 | トレンド確認 |
| TKクロス | 転換線が基準線を上抜け/下抜け | エントリーシグナル |
| 雲（クモ） | SpanA と SpanB の間 | 動的サポート・レジスタンス |

---

## ログ

| プレフィックス | 内容 |
|---|---|
| `[COLLECT]` | ニュース取得・OHLCV取得・テクニカル分析の進捗 |
| `[AGGREGATE]` | テクニカルスナップショットの時間加重集約 |
| `[NEWS]` | ニュース分析結果 |
| `[PRICE]` | 価格分析結果（バイアス・信頼度・RR） |
| `[SIGNAL]` | シグナルスコア内訳・エントリー価格 |
| `[CLOSE]` | SL/TP到達の検出 |
| `[TRADE]` | 注文実行・決済・残高更新 |
| `[ORDER]` | 注文詳細・シグナル理由 |
| `[REFLECT]` | 振り返り結果・方向性正誤・教訓 |

| 出力先 | 内容 |
|---|---|
| ターミナル | `logging.level` で指定したレベル以上（RichHandler） |
| `logs/finance.log` | DEBUG以上の全ログ（ローテーション対応） |
| `logs/activity.log` | 上記プレフィックスのログのみ（取引・ニュース活動専用） |

---

## 状態ファイル

| ファイル | 内容 |
|---|---|
| `data/prices.db` | SQLite — OHLCVキャッシュ + テクニカルスナップショット |
| `data/state/positions.json` | 現在のオープンポジション・残高 |
| `data/state/trades.json` | クローズ済み取引履歴 |
| `data/rag/` | ChromaDB（ニュース・振り返りベクトルDB） |
| `config/user_notes.md` | ユーザーの裁量判断メモ（価格分析プロンプトに注入） |

---

## ユーザーメモ（`config/user_notes.md`）

手動で記述したトレーダーの視点・判断をLLMの価格分析プロンプトに注入できます。

```markdown
<!-- このファイルに書いたテキストが価格分析に反映されます -->

ドル円は150円台の防衛ラインが意識されている。
BOJの追加利上げ観測が強まっている。
```

HTMLコメント・見出し・区切り線は自動的に除去されます。

---

## 注意事項

- **本システムはペーパートレード専用**です。実際の資金取引には使用しないでください。
- yfinance の価格データには最大15分の遅延があります。
- 土日（市場休場）は取引判定を自動スキップします。情報収集ループは継続します。
- RAG の効果はデータ蓄積とともに向上します。運用開始直後は振り返りコンテキストがない状態で動作します。
- テクニカル分析の精度はスナップショットの蓄積とともに向上します。初回起動直後はLLM即時分析にフォールバックします。

---

## 免責事項

本ソフトウェアは教育・研究目的で提供されるものであり、投資助言・売買推奨を目的としたものではありません。

- 本システムの利用によって生じた**いかなる損害・損失**（金銭的損失を含む）についても、開発者は一切の責任を負いません。
- LLM・テクニカル指標による分析結果は将来の相場を保証するものではなく、**投資判断はすべて自己責任**で行ってください。
- 本システムを `trading_mode: "live"` で実際の資金取引に使用することは**非推奨**です。使用する場合は自己の判断と責任においてのみ行ってください。
- 価格データ・ニュースデータの正確性・完全性・適時性について、開発者は保証しません。
