# FX Paper Trading System

ニュース RAG × LLM を組み合わせた FX スウィングトレード自動売買システム（ペーパートレード）。

---

## 概要

| 項目 | 内容 |
|---|---|
| 取引モード | ペーパートレード（模擬） / OANDA本取引（スタブ） |
| 取引スタイル | スウィング（数日〜数週間） |
| 価格データ | yfinance（Yahoo Finance・無料） |
| ニュース取得 | RSS（FX専門・世界情勢・日本情勢）/ Feedly API（オプション） |
| 分析エンジン | LLM（Ollama / Gemini / OpenAI / Claude — 分析種別ごとに個別設定可） |
| ベクトル化 | Ollama `nomic-embed-text`（ローカル） |
| RAGストア | ChromaDB（ローカルファイルDB） |
| 言語 | Python 3.12 |
| パッケージ管理 | uv |

デフォルト構成では外部APIキー不要。Ollama のみですべてローカル動作します。

---

## アーキテクチャ

### 情報収集ループ（15分間隔・タイムゾーン基準の固定時刻）

1. ニュース取得 → LLM センチメント分析 → ChromaDB 蓄積
   - カテゴリ別に独立して実行: **FX専門**（fx） / **世界情勢**（global） / **日本情勢**（japan）
   - 取得元: RSS フィード（デフォルト）または Feedly API（`feedly.enabled: true` で切り替え、カテゴリ単位で選択可能）
   - 取得制限: 1フィードあたり最大5件、カテゴリ合計最大30件
   - 取得テキスト: タイトル + サマリー先頭300文字（全文不要）
   - MD5フィンガープリントで前回と記事セットを比較 → 変化なければLLM呼び出しをスキップ
   - **バッチ分析**: カテゴリ内の全記事を1回のLLM呼び出しで一括分析（記事ごとの個別呼び出しではない）
   - LLM出力: `sentiment_score`(-1.0〜+1.0) / `confidence` / `key_themes` / `bullish_factors` / `bearish_factors` / `summary`
   - nomic-embed-text でベクトル化 → ChromaDB に蓄積
2. yfinance で OHLCV 取得 → SQLite にキャッシュ（差分取得）
   - 期間: `lookback_days`（デフォルト90日）、足種: `ohlcv_interval`（デフォルト1h）
   - SQLiteキャッシュの末尾から差分追記するため毎回全量取得しない
3. テクニカル指標計算（pandas-ta + 一目均衡表 手計算）
   - pandas-ta: SMA(20/50/200), EMA(12/26), RSI(14), MACD(12-26-9), Bollinger Bands(20,2σ), ATR(14), ADX(14)
   - 一目均衡表は手計算: 転換線(9)・基準線(26)・先行スパンA/B・遅行スパン
   - 一目総合判定: 4条件（雲の上下・TKクロス・雲の色・遅行線位置）の合致数で5段階評価
   - トレンド方向: SMA整列（`価格 > SMA20 > SMA50` → uptrend 等）
   - スウィング高値/安値: 直近30本から局所高値・安値を検出
4. LLM でテクニカル分析 → スナップショット保存（SQLite・48時間で自動削除）
   - プロンプト入力: 直近20本のOHLCV + 全指標 + 一目均衡表 + ニュースセンチメント(RAG) + 振り返り教訓(RAG) + user_notes.md
   - LLM出力: `direction_bias`(long/short/neutral) / `bias_score`(-1.0〜+1.0) / `confidence` / `entry_zone` / `stop_loss` / `take_profit` / `risk_reward_ratio` / `reasoning_summary`
   - `temperature: 0.1`（低め）で一貫性重視、`extract_json()` で `<think>` タグ除去後にJSON抽出

### 取引判定ループ（15:00 / 21:30 JST / 土日スキップ）

1. **Phase 1**: 既存ポジションの SL/TP 到達確認・クローズ
2. **Phase 2**: オープンポジションの振り返り生成 → ChromaDB 蓄積
3. **Phase 3**: テクニカルスナップショットを時間加重集約（直近8h）
   - 重み: `1/(1+経過時間[h])` — 直近ほど重く評価（1h前→0.50、3h前→0.25）
   - `bias_score` / `confidence` を加重平均、SL/TP/エントリーゾーンは最新スナップショットの値を使用
   - スナップショット未蓄積時は LLM 即時分析にフォールバック
4. **Phase 4**: RAG からニュースセンチメントを集約
   - シグナル統合（テクニカル60% + ニュース40%）
   - BUY/SELL/HOLD 判定（HOLD時も方向予測を表示）
   - `detail_reason` にニュース/テクニカル内訳を生成（通知に付加）
4a. **Phase 4a**（オプション）: ポジション再評価（Layer 1〜3）
   - **Layer 1**（シグナル反転）: 新シグナルと既存ポジション方向が逆で、新シグナルの信頼度が高い → 早期決済
   - **Layer 2**（タイムアウト）: 保有期間がmax_holding_daysを超えた上、TP方向への進捗が不足 → 損失決済
   - **Layer 3**（利益ロック）: 含み益がある程度進捗した上、シグナル強度が減衰 → 利益確定
5. **Phase 5**: ペーパー注文執行・通知送信・レポート出力
   - BUY/SELL → 注文執行 → 発注通知（判断理由付き）
   - HOLD → スキップ通知（方向予測 + 判断理由）
   - 既存ポジションによりスキップ → スキップ通知（判断理由付き）

### ポジション管理（4層リスク制御）

ポジションの管理・リスク制御は4つのレイヤーで構成されています。

| レイヤー | トリガー | 判定 | 結果 | 実装 |
|---|---|---|---|---|
| **Layer 1** | Phase 4a（取引判定時） | シグナルがポジション方向と反転 + 新信頼度 ≥ 0.70 | 反転信号の即時決済 | `position_reviewer.py` |
| **Layer 2** | Phase 4a（取引判定時） | 保有日数 ≥ max_holding_days + TP進捗 < 30% | タイムアウト損失決済 | `position_reviewer.py` |
| **Layer 3** | Phase 4a（取引判定時） | TP進捗 ≥ 40% + シグナル強度 < 0.15 | 利益ロック決済 | `position_reviewer.py` |
| **Layer 4** | price_monitor（5分ごと） | TP進捗 ≥ 40% | SLをTP方向に追従（トレーリング） | `price_monitor.py` |

- **Layer 1~3**: 取引判定ループ（15:00/21:30 JST）内で実行。Phase 3 で生成されたシグナルを既存ポジションに対して再評価し、早期決済すべきか判断。
- **Layer 4**: 価格監視ジョブ（5分間隔）で常時実行。SL は利益方向にのみ移動（損失方向への移動は無視）し、含み益を保護。

設定は `config/settings.yaml` の以下のセクションで制御:
```yaml
trading:
  position_review_enabled: false              # Layer 1〜3 有効化
  reversal_confidence_min: 0.70               # Layer 1 信頼度閾値
  max_holding_days: 10                        # Layer 2 最大保有日数
  timeout_min_progress_pct: 0.30              # Layer 2 最小進捗率
  profit_lock_min_progress_pct: 0.40          # Layer 3 利益ロック進捗率
  profit_lock_score_floor: 0.15               # Layer 3 シグナル強度フロア

price_monitor:
  trailing_stop_enabled: false                # Layer 4 有効化
  trailing_stop_activation_pct: 0.40          # Layer 4 発動進捗率
  trailing_stop_distance_ratio: 1.0           # Layer 4 SL追従比率
```

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
│   ├── startup.py                  # 起動時チェック（Ollama・シンボル疎通・ディレクトリ）
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
│   │   ├── feedly_fetcher.py       # Feedly API ニュース取得（rss_fetcher と同一インターフェース）
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
│   │   └── price_monitor.py        # オープンポジション価格監視・急変動通知・緊急損切り・トレーリングストップ（Layer 4）
│   ├── signals/
│   │   └── signal_combiner.py      # テクニカル×ニュース シグナル統合
│   ├── trading/
│   │   ├── broker_adapter.py       # BrokerAdapter ABC
│   │   ├── paper_broker.py         # ペーパートレード実装
│   │   ├── paper_trader.py         # 模擬注文・SL/TP判定
│   │   ├── live_broker.py          # OANDA本取引（スタブ）+ ファクトリ
│   │   ├── market_hours.py         # FX市場開閉判定（NY時間基準・DST自動対応）
│   │   ├── position_manager.py     # ポジション・残高・PnL管理
│   │   └── position_reviewer.py    # ポジション再評価（Layer 1〜3）
│   ├── rag/
│   │   ├── vector_store.py         # ChromaDB ラッパー（ニュース・振り返り）
│   │   ├── embedder.py             # nomic-embed-text ベクトル化
│   │   └── prompt_formatter.py     # RAGデータのプロンプト整形
│   ├── api/
│   │   └── server.py               # REST API サーバー（FastAPI + uvicorn）
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

# オンラインLLMを使う場合や REST API を有効化する場合は .env を作成
cp .env.example .env
# .env に以下のキーを必要に応じて設定
# GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY  — オンラインLLM使用時
# API_SECRET_KEY                                        — REST API 有効時
# FEEDLY_ACCESS_TOKEN                                   — Feedly API 使用時
```

### 実行

```bash
uv run python main.py
```

起動時に以下のチェックを実行します:

- **Ollamaモデル**: 設定された全モデル（ニュース/価格/振り返り/embedding）の存在確認
- **シンボル疎通**: `enabled: true` の全銘柄を yfinance で並列チェック（`mode: trade` の失敗は起動ブロック、`watch`/`index` は警告のみ）
- **ディレクトリ**: データ・RAG・ログディレクトリの自動作成

チェック後、ニュース収集を1回実行し、その後スケジュールに従って動作します。

### CLIコマンド

起動後、プロンプト（`>`）からコマンドを入力できます。

| コマンド | 略記 | 内容 |
|---|---|---|
| `status` | `s` | 残高・オープンポジション（含み損益付き）を表示 |
| `run news` | `run n` | ニュース収集を今すぐ実行 |
| `run tech` | `run t` | テクニカル分析を今すぐ実行 |
| `run analyze` | `run a` | 総合分析を今すぐ表示（保存済みデータのみ・新規取得なし） |
| `run mon` | | 価格監視を今すぐ実行 |
| `close <pair>` | | ポジションを手動決済  例: `close USDJPY=X` |
| `notify` | `n` | 通知テストメッセージを送信 |
| `edit` | `e` | `user_notes.md` を vim で編集 |
| `help` | `h` | コマンド一覧を表示 |
| `quit` | `q` | 終了 |

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

### 銘柄設定（`instruments`）

FX通貨ペアと株価指数を統一した `instruments` リストで管理します。

```yaml
instruments:
  # ── FX通貨ペア ──
  - symbol: "USDJPY=X"              # yfinance シンボル
    display_name: "USD/JPY"
    asset_type: fx                  # "fx" | "index"
    mode: trade                     # "trade"（取引）| "watch"（監視のみ）
    pip_value: 0.01
    base_currency: "USD"
    quote_currency: "JPY"
    enabled: true                   # false で無効化（起動チェックもスキップ）

  # ── 株価指数（監視専用） ──
  - symbol: "^N225"
    display_name: "Nikkei 225"
    asset_type: index
    currency: "JPY"                 # 関連通貨（ニュースカテゴリ紐付け用）
    enabled: true
```

| `mode` | 動作 |
|---|---|
| `trade` | OHLCV取得 + テクニカル分析 + シグナル生成 + 注文（`asset_type: fx` のみ） |
| `watch` | OHLCV取得 + テクニカル分析のみ（参照銘柄） |

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
  notify_on_order_open: true         # 注文発注時（判断理由付き）
  notify_on_order_close: true        # 決済時（TP/SL/緊急損切り）
  notify_on_signal_skipped: true     # スキップ（既存ポジション）・HOLD（不参加）時
  notify_on_price_alert: true        # 損失方向への価格急変動
```

通知には**判断理由**（ニューススコア・テクニカルスコア内訳・合成スコア）が自動付加されます。

| 通知種別 | トリガー | 内容 |
|---|---|---|
| 注文発注 | BUY/SELL シグナル → 注文執行 | エントリー/SL/TP/サイズ + 判断理由 |
| 決済 | SL/TP到達 または 緊急損切り | PnL・残高 |
| スキップ | シグナル発生だが既存ポジションあり | 方向・確信度 + 判断理由 |
| HOLD | シグナルなし（不参加） | 方向予測（bullish/bearish/neutral 寄り）+ 判断理由 |
| 価格急変動 | 損失 `alert_threshold_pct` 超過 | 損失率・SLまでの距離 |

### REST API

```yaml
api:
  enabled: false              # true で REST API サーバーを起動
  port: 8811                  # リッスンポート
  # 認証: .env の API_SECRET_KEY を設定（X-API-Key ヘッダーで送信）
```

---

## REST API

`api.enabled: true` を設定すると FastAPI + uvicorn によるREST APIサーバーが起動します。
外部ツール・スクリプトからシステム状態の確認や緊急操作が可能になります。
Discord/Telegram通知（プッシュ型・イベント駆動）とは独立した、プル型の操作インターフェースです。

### セットアップ

```bash
# .env に認証キーを設定
echo "API_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")" >> .env
```

```yaml
# config/settings.yaml
api:
  enabled: true
  port: 8811   # リッスンポート（0.0.0.0 固定）
```

すべてのリクエストで `X-API-Key` ヘッダーが必要です。

### エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/health` | 起動確認・スケジューラ状態（ジョブ数・次回実行時刻） |
| `GET` | `/status` | 残高・PnL・勝率・オープンポジション（含み損益付き） |
| `GET` | `/news` | カテゴリ別（fx/global/japan）の最新ニュースセンチメント（`run news` と同等） |
| `GET` | `/tech` | 銘柄別の最新テクニカルスナップショット（`run tech` と同等） |
| `GET` | `/analyze` | 保存済みデータからの総合シグナル（`run analyze` と同等） |
| `POST` | `/close/{pair}` | ポジションを即時決済 |

### 利用例

```bash
KEY="your_api_secret_key"
HOST="http://localhost:8811"

# 死活確認
curl -H "X-API-Key: $KEY" $HOST/health

# ポジション・残高確認
curl -H "X-API-Key: $KEY" $HOST/status

# ニュースセンチメント確認
curl -H "X-API-Key: $KEY" $HOST/news

# テクニカルスナップショット確認
curl -H "X-API-Key: $KEY" $HOST/tech

# 総合シグナル確認
curl -H "X-API-Key: $KEY" $HOST/analyze

# 緊急決済（USDJPY=X をクローズ）
curl -X POST -H "X-API-Key: $KEY" "$HOST/close/USDJPY%3DX"
```

---

## ニュースソース

### RSS フィード（デフォルト）

| カテゴリ | ソース |
|---|---|
| FX専門 | ForexLive, FXStreet, Investing.com Forex |
| 世界情勢・株価 | Reuters Business, BBC Business, AP Business, Financial Times, Yahoo Finance, Investing.com Stock News |
| 日本情勢・株価 | NHK World, Japan Today, Japan Times, Nikkei Asia, 日本経済新聞（マーケット） |

JPYを含む通貨ペアでは日本情勢・株価フィードが自動的に追加されます。

### Feedly API（オプション）

Feedly の Personal Access Token を設定すると、RSS の代わりに Feedly のカテゴリ（整理済みフィード）からニュースを取得できます。RSS と Feedly はカテゴリ単位で切り替え可能です。

```yaml
# config/settings.yaml
news_sources:
  feedly:
    enabled: true
    access_token: ""          # settings.yaml に直接記載、または .env の FEEDLY_ACCESS_TOKEN
    streams_fx:               # カテゴリの Stream ID（GET /v3/categories で確認）
      - "user/xxx.../category/FX"
    streams_global:
      - "user/xxx.../category/Global"
    streams_japan: []         # 空リストの場合そのカテゴリは RSS にフォールバック
    count: 20                 # 1ストリームあたりの取得件数上限
```

Stream ID の取得方法:

```bash
curl -H "Authorization: Bearer <access_token>" https://cloud.feedly.com/v3/categories
# → 各カテゴリの "id" フィールドが Stream ID
```

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

### ログローテーション

```yaml
logging:
  level: "INFO"
  file: "logs/finance.log"
  activity_log_file: "logs/activity.log"
  rotate_timing: "10MB"   # サイズ: 10MB / 512KB など
                          # 時間間隔: 6H（毎6時間）/ 1D（毎日）
                          # タイミング: midnight（日次0時）/ W0〜W6（曜日）
  backup_count: 5
```

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
