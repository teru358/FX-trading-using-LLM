# FX Paper Trading System — 詳細ドキュメント

## 情報収集ループ（15分間隔）

1. **ニュース取得 → LLMセンチメント分析 → ChromaDB蓄積**
   - カテゴリ別に独立実行: FX専門(fx) / 世界情勢(global) / 日本情勢(japan)
   - RSS フィード（デフォルト）または Feedly API（カテゴリ単位で選択可能）
   - 1フィードあたり最大5件、カテゴリ合計最大30件
   - MD5フィンガープリントで前回と比較 → 変化なければLLMスキップ
   - バッチ分析: カテゴリ内の全記事を1回のLLM呼び出しで一括分析
   - 方向別RAG（bullish/bearish）から過去の取引教訓を注入
   - JSON出力失敗時は最大2回リトライ

2. **OHLCV取得 → SQLiteキャッシュ（差分取得）**
   - 期間: `lookback_days`（デフォルト60日）、足種: `ohlcv_interval`（デフォルト1h）
   - 重複タイムスタンプの自動除去

3. **テクニカル指標計算 → LLM分析 → スナップショット保存**
   - 2フェーズ実行: Phase 1(watch銘柄) → Phase 1.5(相関計算) → Phase 2(trade銘柄)
   - watch銘柄の分析結果をマクロコンテキストとしてtrade銘柄に付加
   - trade×watch銘柄間の20本ローリング相関係数を算出し、定量データとしてLLMに提供
   - 価格データが6時間以上古い場合はLLM分析をスキップ（閉場時の無駄なコスト抑止）

## 予測サイクル（2時間間隔・LLMなし）

1. **Phase 1**: 直近24hの全予測を毎サイクル更新検証
   - 有意な変動（ATR(14) × `forecast_significance_atr_ratio` 以上）があれば集計サマリーをRAGにupsert
2. **Phase 2**: 新規予測生成
   - 蓄積済みテクニカルスナップショットからシグナル合成（LLM呼び出しなし）
   - `|combined_score| < forecast_min_combined_score` → スキップレコードを保存

ノイズ対策: ATR有意性フィルター / 検証ウィンドウ制御 / スコア閾値 / RAGには事実文字列のみ蓄積

## 取引判定ループ（1日6回）

1. **Phase 1**: SL/TP到達確認・クローズ + ATRパラメータ提案の処理
2. **Phase 2**: オープンポジションの振り返り生成 → ChromaDB蓄積（方向別bullish/bearish）
3. **Phase 3**: テクニカルスナップショットを時間加重集約（直近8h）
   - 重み: `1/(1+経過時間[h])` — 直近ほど重く評価
   - 方向一致性の時間減衰を信頼度に反映
4. **Phase 4**: ニュースセンチメント集約 + シグナル統合
   - テクニカル60% + ニュース40%（動的重み調整: ニュース信頼度≥0.80で+0.10）
   - 方向対立時のスケーリングペナルティ（弱い信号の信頼度に基づく）
   - RAG方向別スコア補正（bullish/bearishコレクションから類似パターン検索）
4. **Phase 4a**: ポジション再評価（Layer 1〜3、オプション）
5. **Phase 5**: ATRベースSL/TP算出 → 注文執行 → 通知

## ポジション管理（4層リスク制御）

| レイヤー | トリガー | 判定 | 結果 |
|---|---|---|---|
| **Layer 1** | 取引判定時 | シグナル反転 + 信頼度 ≥ 0.70 | 早期決済 |
| **Layer 2** | 取引判定時 | 保有日数超過 + TP進捗不足 | タイムアウト決済 |
| **Layer 3** | 取引判定時 | 含み益進捗 + シグナル減衰 | 利益ロック |
| **Layer 4** | 価格監視(10分) | TP進捗 ≥ 40% | トレーリングストップ |

## ATRベースSL/TP

LLM依存のSL/TP出力（0.2-5pips等の異常値）を置き換え:

- SL = ATR(14) × `sl_atr_mult_default` (デフォルト1.5)
- TP = ATR(14) × `tp_atr_mult_default` (デフォルト3.0)
- LLM提案値との比較を記録し、振り返り時に`adaptive_params.yaml`へフィードバック
- ペア別ATR倍率の動的調整（±0.5 delta制限、min/maxクランプ、10件履歴保持）

## 相関分析

テクニカル分析Phase 1.5でtrade×watch銘柄の価格相関を定量計算:

```
=== Price Correlation (20-bar rolling) ===
Gold (GLD ETF)   r=-0.650 (moderate negative) prev=-0.400 Δ=-0.250 ⚠ DIVERGING
S&P 500 (SPY)    r=+0.350 (weak positive)    prev=+0.380 Δ=-0.030
```

- 相関変化 Δ≥0.2 でアラート表示
- LLMプロンプトにガイドライン付きで提供

## シグナルロジック

```
combined_score = テクニカルスコア × price_weight + ニューススコア × news_weight

# ニュースとテクニカルが逆方向の場合: スケーリングペナルティ
conflict_penalty = 1.0 - (0.5 × min(news.conf, price.conf))

# 判定
score > +signal_deadband かつ confidence >= threshold → BUY
score < -signal_deadband かつ confidence >= threshold → SELL
それ以外 → HOLD（方向予測は表示）

# ポジションサイジング
lot = (残高 × risk_per_trade) ÷ ストップ幅(pips)
```

## テクニカル指標

| 指標 | パラメータ | 用途 |
|---|---|---|
| SMA | 20/50/200 | トレンド方向 |
| EMA | 12/26 | MACD計算用 |
| RSI | 14 | 過熱感 |
| MACD | 12-26-9 | モメンタム転換 |
| Bollinger Bands | 20日・2σ | 価格位置（%B） |
| ATR | 14 | ボラティリティ・SL/TP算出 |
| ADX | 14 | トレンド強度 |
| 一目均衡表 | 9/26/52 | トレンド・サポレジ統合判断 |

### チャートパターン検出（15パターン）

| カテゴリ | パターン | デフォルト |
|---|---|---|
| ローソク足 | ハンマー・シューティングスター・エンガルフィング・十字線・明星/宵星・三白兵/三黒兵・ピンバー・インサイドバー | 有効 |
| チャート形状 | ダブルトップ/ボトム・ヘッドアンドショルダー・三角保ち合い・レンジ | 無効 |
| ブレイクアウト | BBスクイーズ・ATR収縮・サポレジ抜け | 無効 |

## 価格データプロバイダー

| 項目 | yfinance | Twelve Data |
|---|---|---|
| コスト | 無料 | 無料（800 req/日） |
| FX遅延 | 15-20分 | リアルタイム |
| 対象 | 全銘柄 | trade銘柄 + watch_symbols指定銘柄 |
| フォールバック | — | yfinance自動切替 |

watch銘柄はETFシンボルを使用（yfinance安定性のため）:

| 対象 | シンボル | 備考 |
|---|---|---|
| S&P 500 | SPY | ETF |
| 日経225 | 1321.T | 東証ETF |
| ドル指数 | UUP | ETF |
| Gold | GLD | Twelve Data対応 |
| 長期債ETF | IEF | 独立銘柄（旧^TNX利回りとは非連続） |
| 短期債ETF | SHY | 独立銘柄（旧^IRX利回りとは非連続） |
| 原油 | USO | ETF |

## LLMプロバイダー

| プロバイダー | 設定値 | 必要な環境変数 | デフォルトモデル |
|---|---|---|---|
| Ollama | `"ollama"` | なし | `llama3.1:8b` |
| Gemini | `"gemini"` | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| OpenAI | `"openai"` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| Claude | `"claude"` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` |

3種類の分析（news/price/reflection）それぞれに異なるプロバイダー・モデルを設定可能。

## 通知

Discord / Telegram / None から選択。全通知にサイクル種別ラベル付き。

| 通知種別 | トリガー | 内容 |
|---|---|---|
| 注文発注 | BUY/SELL → 注文執行 | [取引サイクル] エントリー/SL/TP + 判断理由 |
| 決済 | SL/TP到達・緊急損切り | [決済判定/価格監視] PnL・残高 |
| スキップ | 既存ポジションあり | [取引サイクル] 方向・確信度 + 理由 |
| HOLD | シグナルなし | [取引サイクル] 方向予測 + 理由 |
| 価格急変動 | 損失閾値超過 | [価格監視] 損失率・SLまでの距離 |

## REST API

`api.enabled: true` で FastAPI + uvicorn が起動。認証: `X-API-Key` ヘッダー。

```bash
KEY="your_api_secret_key"
HOST="http://localhost:8811"

curl -H "X-API-Key: $KEY" $HOST/health
curl -H "X-API-Key: $KEY" $HOST/status
curl -H "X-API-Key: $KEY" $HOST/schedule
curl -H "X-API-Key: $KEY" $HOST/news
curl -H "X-API-Key: $KEY" $HOST/tech
curl -H "X-API-Key: $KEY" $HOST/analyze
curl -X POST -H "X-API-Key: $KEY" "$HOST/close/USDJPY%3DX"
```

## ディレクトリ構成

```
finance/
├── main.py                         # エントリーポイント・スケジューラ
├── config/
│   ├── settings.yaml               # 運用パラメータ
│   ├── instruments.yaml            # 銘柄 + price_provider
│   ├── news_sources.yaml           # キーワード + フィード
│   └── user_notes.md               # ユーザーメモ（LLMプロンプトに注入）
├── src/
│   ├── config.py                   # 設定ローダー（3ファイルマージ）
│   ├── trading_cycle.py            # 取引サイクル オーケストレータ
│   ├── llm/                        # LLMクライアント（Ollama/Gemini/OpenAI/Claude）
│   ├── analysis/                   # ニュース・テクニカル分析・振り返り
│   ├── data/                       # OHLCV・指標・相関・セッション管理
│   ├── jobs/                       # 収集ジョブ（ニュース/テクニカル/価格監視）
│   ├── signals/                    # シグナル統合・RAG補正
│   ├── trading/                    # ブローカー・ATR算出・ポジション管理
│   ├── rag/                        # ChromaDB・方向別ストア・セマンティック検索
│   ├── api/                        # REST API (FastAPI)
│   ├── notifications/              # Discord/Telegram通知
│   └── persistence/                # 状態管理・動的パラメータ
├── data/
│   ├── prices.db                   # SQLite（OHLCV + スナップショット + セッション）
│   ├── state/                      # ポジション・取引履歴 + adaptive_params.yaml
│   └── rag/                        # ChromaDB
└── logs/
    ├── finance.log                 # 全ログ（ローテーション対応）
    └── activity.log                # 取引・ニュース活動ログ
```

## ログプレフィックス

| プレフィックス | 内容 |
|---|---|
| `[COLLECT]` | ニュース・OHLCV取得・テクニカル分析 |
| `[CORR]` | 銘柄間相関計算 |
| `[AGGREGATE]` | スナップショット時間加重集約 |
| `[NEWS]` | ニュース分析結果 |
| `[PRICE]` | 価格分析結果 |
| `[SIGNAL]` | シグナルスコア内訳 |
| `[CLOSE]` | SL/TP到達検出 |
| `[TRADE]` | 注文実行・決済 |
| `[REFLECT]` | 振り返り結果 |
| `[FORECAST]` | 予測サイクル |
| `[EXIT]` | ポジション再評価決済 (Layer 1-3) |
| `[MONITOR]` | 価格監視・急変動・トレーリング |

## 状態ファイル

| ファイル | 内容 |
|---|---|
| `data/prices.db` | OHLCV + スナップショット + 予測 + セッション |
| `data/state/positions.json` | オープンポジション・残高 |
| `data/state/trades.json` | クローズ済み取引履歴 |
| `data/state/adaptive_params.yaml` | ペア別ATR倍率（動的更新） |
| `data/rag/` | ChromaDB（ニュース・振り返り・方向別） |
