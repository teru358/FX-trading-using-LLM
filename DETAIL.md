# FX Trading Bot — 詳細ドキュメント

このドキュメントは [README.md](README.md) の補足です。アーキテクチャ詳細・各サブシステムのロジック・状態管理を扱います。

## 目次

- [運用モードとブローカー](#運用モードとブローカー)
- [スケジューラと各サイクル](#スケジューラと各サイクル)
- [情報収集ループ](#情報収集ループ)
- [予測サイクル](#予測サイクル)
- [取引判定ループ](#取引判定ループ)
- [exit_check サイクル](#exit_check-サイクル)
- [価格監視ループ](#価格監視ループ)
- [ポジション管理 (5 層)](#ポジション管理-5-層)
- [シグナルロジック](#シグナルロジック)
- [テクニカル指標](#テクニカル指標)
- [LLM プロバイダーと耐障害性](#llm-プロバイダーと耐障害性)
- [価格データプロバイダー](#価格データプロバイダー)
- [MT5 ブリッジ連携](#mt5-ブリッジ連携)
- [halt 制御](#halt-制御)
- [通知](#通知)
- [REST API](#rest-api)
- [reviewer ガードと監視機構](#reviewer-ガードと監視機構)
- [ディレクトリ構成](#ディレクトリ構成)
- [状態ファイル](#状態ファイル)
- [休場時の動作](#休場時の動作)
- [ログプレフィックス](#ログプレフィックス)

## 運用モードとブローカー

`config/settings.yaml` の 3 フィールドで決定:

| `mode` | `paper_provider` | `live_broker` | primary broker | shadow / 副系統 |
|---|---|---|---|---|
| `paper` | `twelvedata` or `yfinance` | (任意) | `PaperBrokerAdapter` (内部 PositionManager) | なし |
| `live_test` | `twelvedata` | `mt5` | `PaperBrokerAdapter` (内部 PositionManager) | `LiveTestObserver` (MT5 ticket / fill を shadow 記録) |
| `live` | (使用しない) | `mt5` | `Mt5BridgeBrokerAdapter` (MT5 経由実発注) | なし |

`Mt5BridgeBrokerAdapter` は `bridge_url`、`api_key`、`magic_number` 等で接続。`order_id` は `"mt5:<ticket>"` プレフィックスで MT5 側 ticket と紐付け。reconciliation で MT5 側ポジションと内部状態の整合性を毎サイクル検証。

## スケジューラと各サイクル

`schedule` ライブラリで Asia/Tokyo タイムゾーン基準。`main.py` で全ジョブを登録。

| サイクル | 起動 | LLM | 主な処理 |
|---|---|---|---|
| 価格監視 | `price_monitor.interval_minutes` | なし | MFE/R更新 / Profit Protection / remote SL sync / 急変動通知 / emergency_close |
| exit_check | 毎時 :00 | なし | SL/TP 確認 / reconciliation / Reversal Guard / Time Stop 再評価 (オプション) |
| ニュース収集 | `news_collection.interval_minutes` | あり | RSS/Feedly 取得 + センチメント分析 |
| RAG クリーンアップ | 1 日 1 回 | なし | 古い記事の削除 |
| テクニカル分析 | 毎時 :00 | あり | OHLCV + 指標 + LLM スコアリング |
| 予測サイクル | `analysis.forecast_review_interval_hours` | なし | 予測精度検証 + 新規予測生成 |
| 取引判定 | `schedule.run_times` で指定 | あり | bridge gate + シグナル統合 + 発注 |
| 経済指標フェッチ | 1 日 1 回 | なし | calendar 取得 |
| Performance audit | 週 1 (任意) | あり | 統計診断 + 改善提案 |

## 情報収集ループ

### ニュース取得 → LLM センチメント分析 → ChromaDB 蓄積

- カテゴリ別に独立実行: FX 専門 (fx) / 世界情勢 (global) / 日本情勢 (japan)
- RSS フィード (デフォルト) または Feedly API (カテゴリ単位で選択可能)
- 1 フィードあたり最大 5 件、カテゴリ合計最大 30 件
- MD5 フィンガープリントで前回と比較 → 変化なければ LLM スキップ
- バッチ分析: カテゴリ内の全記事を 1 回の LLM 呼び出しで一括分析
- 方向別 RAG (bullish/bearish) から過去の取引教訓を注入
- JSON 出力失敗時は最大 2 回リトライ
- サーキットブレーカー: 連続 3 回失敗で LLM 呼び出しを 300 秒スキップ (自動復帰)

### OHLCV 取得 → SQLite キャッシュ (差分取得)

- 期間: `lookback_days`、足種: `ohlcv_interval`
- 重複タイムスタンプの自動除去
- live モードでは MT5 ティック由来、それ以外は yfinance / TwelveData

### テクニカル指標計算 → ルールベーススコアリング → スナップショット保存

- 2 フェーズ実行: Phase 1 (watch 銘柄) → Phase 1.5 (相関計算) → Phase 2 (trade 銘柄)
- watch 銘柄の分析結果をマクロコンテキストとして trade 銘柄に付加
- trade × watch 銘柄間の 20 本ローリング相関係数を算出し、定量データとして LLM に提供
- 価格データが 6 時間以上古い場合は LLM 分析をスキップ (閉場時の無駄なコスト抑止)
- ルールベーススコアリング: SMA/RSI/MACD/Ichimoku/BB/Pattern の 6 カテゴリ重み付き合計 × ADX フィルター
- スナップショット保存時に `collect_status` sentinel を記録 (`ok` / `stale_price` / `failed`)

## 予測サイクル

LLM 不使用。蓄積済テクニカルスナップショットからシグナル合成 → 精度検証 → RAG 蓄積。

1. **Phase 1**: 直近 24h の全予測を毎サイクル更新検証
   - 有意な変動 (ATR(14) × `forecast_significance_atr_ratio` 以上) があれば集計サマリーを RAG に upsert
2. **Phase 2**: 新規予測生成
   - 蓄積済みテクニカルスナップショットからシグナル合成
   - `|combined_score| < forecast_min_combined_score` → スキップレコードを保存

ノイズ対策: ATR 有意性フィルター / 検証ウィンドウ制御 / スコア閾値 / RAG には事実文字列のみ蓄積。

## 取引判定ループ

1. **bridge_health_gate.probe**: `/health` 確認 (1 min retry 含む 2 回失敗で auto soft halt)
2. **Phase 1**: SL/TP 到達確認・クローズ + reconciliation (MT5 と内部状態の整合性)
3. **Phase 1.5**: 決済済みオーダーの振り返り生成 → マクロコンテキスト注入 → adaptive params 更新 → ChromaDB 蓄積
4. **Phase 2.5**: 前回 HOLD の検証 (LLM なし)
5. **Phase 3**: テクニカルスナップショットを指数減衰加重で集約 (直近 8h、最大 16 件)
   - 重み: `exp(-0.693 × 経過時間[h] / 2.0)` — 半減期 2 時間
   - 方向一致性 (consistency) を信頼度に反映
6. **Phase 4**: ニュースセンチメント集約 (confidence 加重平均) + シグナル統合
   - テクニカル × `price_weight` + ニュース × `news_weight`
   - 方向対立時のスケーリングペナルティ
   - RAG 方向別スコア補正 (bullish/bearish コレクションから類似パターン検索)
7. **Phase 4a**: ポジション再評価 (Reversal Guard / Time Stop、`position_review_enabled` で有効化)
8. **Phase 4b**: ポートフォリオガード → ATR ベース SL/TP 算出 → 注文執行 → 通知

## exit_check サイクル

毎時 :00 起動・LLM 不使用・軽量サイクル。新規発注はしない。

1. **SL/TP 確認**: `broker.check_and_close_positions` 経由
2. **Reconciliation** (mt5_bridge モード時): MT5 側ポジションと内部状態を照合し、不整合 (完全 close / 部分 close / orphan) を処理
3. **既存ポジション再評価** (`position_review_enabled=true` 時のみ): Reversal Guard のpending SL target記録 / Time Stop close判定

reviewer の close 実行には [reviewer ガードと監視機構](#reviewer-ガードと監視機構) のチェックが入る。

## 価格監視ループ

`price_monitor.interval_minutes` 間隔。MFE/R更新、Profit Protection、remote SL sync、急変動アラートを担当。

- **急変動通知**: 損失率が `alert_threshold_pct` 超で初回通知、以降 `alert_step_pct` 刻みで再通知
- **emergency_close** (任意): `emergency_close_pct` 損失到達で自動 close。`enable_emergency_close: true` で発動。live モードでは price source が MT5 でないと skip
- **Profit Protection** (`profit_protection_enabled: true` 時): MFE/R と giveback をもとに SL を段階的に引き上げ
  - `protect_half_r` 到達 → SL = entry と元 SL の中間
  - `protect_breakeven_r` 到達 → SL = entry (breakeven)
  - `protect_lock_r` 到達 → SL = +0.3R 相当の利益確保位置
- **pending protection target**: Reversal Guard が記録した保護SLを適用
- **remote_sl_sync** (任意, `remote_sl_sync_enabled: true` 時): SL 更新を MT5 server-side にも反映し、成功後に内部SLを更新
- **Legacy trailing** (`trailing_stop_enabled: true` 時): 旧TP進捗ベースのtrailing helperも互換目的で残存

## ポジション管理 (5 層)

| レイヤー | トリガー | 判定 | 結果 |
|---|---|---|---|
| **Layer 0: Portfolio Guard** | 発注前 | 通貨グループ別ポジション集中・同一ペア上限 | 発注スキップ |
| **Layer 1: Server SL/TP** | MT5 server-side | SL/TP 到達 | MT5が決済、financeはreconciliationで検知 |
| **Layer 2: Reversal Guard** | exit_check :00 / 取引判定 | 反転シグナル + 信頼度 + 最小保有時間 | 原則closeせずpending protection SLを記録 |
| **Layer 3: Time Stop** | exit_check :00 / 取引判定 | 最大保有時間 / no-progress MFE不足 | timeout close |
| **Layer 4: Profit Protection** | 価格監視 | MFE/R到達・giveback | SL引き上げ / remote SL sync |
| **Layer 5: Emergency Guard** | 価格監視 | 損失方向の急変 | emergency close / degraded alert |

Reversal Guard / Time Stop は `position_review_enabled` で有効化する。Profit Protection は `profit_protection_enabled`、legacy trailing は `trailing_stop_enabled`、remote SL同期は `remote_sl_sync_enabled` で制御する。旧 `Layer 1-3` / `profit_lock` ラベルは過去ログ・互換close_reasonとして残る場合がある。

### ポートフォリオガード

通貨グループ (JPY/USD/EUR/GBP) ごとに相関の高いペアへの過剰集中を防止:

| 設定 | 内容 |
|---|---|
| `max_total_positions` | 全体の最大同時ポジション数 |
| `max_positions_per_currency_group` | 通貨グループ別の最大ポジション数 |
| `max_same_direction_per_group` | グループ内同方向の最大ポジション数 |
| `max_positions_per_pair` | 同一ペアの最大同時ポジション数 (scale_in 用) |

## シグナルロジック

```
combined_score = テクニカルスコア × price_weight + ニューススコア × news_weight

# ニュースセンチメント集約: confidence 加重平均
avg_score = Σ(score_i × conf_i) / Σ(conf_i)

# ニュースとテクニカルが逆方向の場合: スケーリングペナルティ
conflict_penalty = 1.0 - (0.3 × min(news.conf, price.conf))

# RAG 方向別補正 (オプション)
adjustment = 補正係数 × 同方向過去事例数 / 全事例数  (上限 ±rag_adjustment_max)

# 判定
score > +signal_deadband かつ confidence >= threshold → BUY
score < -signal_deadband かつ confidence >= threshold → SELL
それ以外 → HOLD (方向予測は表示)

# ポジションサイジング
lot = (残高 × risk_per_trade) ÷ ストップ幅(pips)
lot = max(lot, min_lot_size) / lot_unit  → 切り捨て
```

## テクニカル指標

| 指標 | パラメータ | 用途 | スコア重み |
|---|---|---|---|
| SMA | 20/50/200 | トレンド方向 | 0.20 |
| RSI | 14 | 過熱感 | 0.15 |
| MACD | 12-26-9 | モメンタム転換 | 0.15 |
| 一目均衡表 | 9/26/52 | トレンド・サポレジ統合判断 | 0.25 |
| Bollinger Bands | 20 日 ・2σ | 価格位置 (%B) | 0.10 |
| チャートパターン | — | 反転・継続シグナル | 0.15 |
| ATR | 14 | ボラティリティ・SL/TP 算出 | — |
| ADX | 14 | トレンド強度 (スコアフィルター) | — |

### マルチタイムフレーム合成

`analysis.multi_timeframe` で 3 つの足種をブレンド可能 (`enabled: true` で有効)。各足種に `lookback_days` / `interval` / `weights` を設定。

### チャートパターン検出

| カテゴリ | パターン |
|---|---|
| ローソク足 | ハンマー・シューティングスター・エンガルフィング・十字線・明星/宵星・三白兵/三黒兵・ピンバー・インサイドバー |
| チャート形状 | ダブルトップ/ボトム・ヘッドアンドショルダー・三角保ち合い・レンジ |
| ブレイクアウト | BB スクイーズ・ATR 収縮・サポレジ抜け |

### ATR ベース SL/TP

- SL = ATR(14) × `sl_atr_mult_default`
- TP = ATR(14) × `tp_atr_mult_default`
- ペア別 ATR 倍率の動的調整 (±0.5 delta 制限、min/max クランプ、履歴保持)
- 振り返り時の `adaptive_params.yaml` フィードバックで自己更新

### 相関分析

テクニカル分析 Phase 1.5 で trade × watch 銘柄の価格相関を定量計算 (20 本ローリング)。

```
=== Price Correlation (20-bar rolling) ===
Gold (GLD ETF)   r=-0.650 (moderate negative) prev=-0.400 Δ=-0.250 ⚠ DIVERGING
S&P 500 (SPY)    r=+0.350 (weak positive)    prev=+0.380 Δ=-0.030
```

相関変化 Δ ≥ 0.2 でアラート、LLM プロンプトにガイドライン付きで提供。

## LLM プロバイダーと耐障害性

`llm.provider` で全役割共通の provider を選択 (役割ごとの model だけ変えられる):

| プロバイダー | 設定値 | 必要な環境変数 |
|---|---|---|
| Claude (CLI) | `"claude-cli"` | `claude` CLI install |
| Claude (API) | `"claude"` | `ANTHROPIC_API_KEY` |
| Gemini | `"gemini"` | `GEMINI_API_KEY` |
| OpenAI | `"openai"` | `OPENAI_API_KEY` |
| Ollama | `"ollama"` | なし (base_url 必須) |
| llama.cpp | `"llamacpp"` | base_url 必須 |

3 つの役割 (`news_analysis` / `price_analysis` / `reflection`) に異なるモデル・温度を設定可能。

### サーキットブレーカー

| 状態 | 動作 |
|---|---|
| **CLOSED** | 正常。呼び出し許可 |
| **OPEN** | 連続 3 回失敗後。300 秒間すべての呼び出しをスキップ (`CircuitOpenError`) |
| **HALF_OPEN** | クールダウン経過。1 回だけ試行を許可。成功すれば CLOSED に復帰 |

## 価格データプロバイダー

| 項目 | yfinance | Twelve Data | MT5 (bridge) |
|---|---|---|---|
| コスト | 無料 | 無料 (日次上限あり) | 自前 MT5 口座 |
| FX 遅延 | 15-20 分 | リアルタイム | リアルタイム (tick) |
| 対象 | 全銘柄 | trade + watch_symbols 指定 | live モードの trade FX のみ |
| フォールバック | — | yfinance 自動切替 | TwelveData → yfinance |

`PriceProvider.get_current_price()` のチェーン: MT5 → TwelveData → yfinance。`CurrentPrice.source` に最終的に使われた source 名が入り、reviewer ガード等の判定材料になる。

## MT5 ブリッジ連携

別プロセス `mt5_bridge` と HTTP/JSON で通信。

### 主要 endpoint

| メソッド | パス | 用途 |
|---|---|---|
| `GET` | `/health` | bridge 死活 + MT5 接続状態 |
| `GET` | `/account` | MT5 口座残高 / equity / margin |
| `GET` | `/positions` | 現在のオープンポジション一覧 |
| `POST` | `/orders` | 新規発注 |
| `POST` | `/positions/{ticket}/close` | ticket 指定 close |
| `POST` | `/positions/{ticket}/modify` | SL/TP 変更 |
| `GET` | `/admin/status` | DRY_RUN / soft_halted / hard_halted / accepts_new_orders |
| `POST` | `/admin/halt` | soft / hard halt 発動 |
| `POST` | `/admin/resume` | soft halt 解除 |

### bridge_health_gate

取引判定・価格監視サイクルの起動時に bridge `/health` をプローブ。失敗 → 1 分後 retry → さらに失敗で auto soft halt 発動。

### reconciliation

`Mt5BridgeBrokerAdapter.check_and_close_positions` が以下を毎サイクル検証:

| 検出パターン | 処理 |
|---|---|
| 内部 open + MT5 不在 (full close) | 内部 close `close_reason="server_sl_tp"` |
| 1% 未満の volume 差 | 端数として無視 |
| 1-30% の部分 close | `adjust_position_size` で内部追従 |
| 30% 以上の volume 消失 | hard halt 発動 |
| MT5 にあるが内部に無い (orphan, bot magic) | hard halt 発動 |
| 他 magic のポジション | キャッシュに保持、干渉せず |
| bridge 不通 | ノーオペ + 永続カウンタ +1 |

### shadow reconciliation stale 監視

bridge 不通による reconciliation skip 連続回数を `state_dir/reconciliation.json` に永続化:

```json
{
  "skipped_consecutive": 0,
  "stale_warned": false,
  "last_skip_at": null,
  "last_recovered_at": null
}
```

閾値到達で警告ログ 1 回 (重複抑止)。`/status` の `mt5_bridge.reconciliation_skipped_consecutive` で監視可能。再 reconciliation 成功で 0 にリセット。

## halt 制御

`data/state/halt.json` を権威的ストアとする (paper モードでは不在 = halted=False)。

### auto halt 発動条件

| トリガー | reason の例 |
|---|---|
| bridge `/health` 2 連続失敗 | `trading: /health failed twice (...)` |
| 注文連続 REJECT | `order_reject_threshold_exceeded` |
| balance divergence の閾値超過 (継続) | `balance_divergence_persistent` |
| balance.json 破損 | `balance_snapshot_corrupted` |
| collect_status sentinel 連続失敗 | `price_provider_unavailable` |
| halt.json 破損 | `halt.json corrupted (...)` (fail-safe halted) |

### manual halt / resume

```bash
# CLI
?halt soft 理由メッセージ
?resume

# REST API
curl -X POST -H "X-API-Key: $KEY" -d '{"mode":"soft","reason":"manual"}' $HOST/admin/halt
curl -X POST -H "X-API-Key: $KEY" $HOST/admin/resume
```

`?resume` (`/admin/resume`) は bridge `/health` + `/admin/status` を同期確認した上で finance halt をクリアする。bridge accepts_new_orders=true (live) or soft_halted=false + mt5_connected=true (live_test) が確認できないと resume 失敗。

### halt の意味論

- soft halt = **新規発注のみ停止**、既存ポジション管理 (SL/TP・トレーリング・reconciliation) は継続
- hard halt = bridge プロセス側で DRY_RUN 強制 + 全 close 試行 (mt5_bridge 仕様)
- `allows_position_management: True` は常時 (= halt は全停止ではない、の明示)

## 通知

Discord Webhook。`notifier.enabled: true` で有効化。

| 通知種別 | トリガー | 内容 |
|---|---|---|
| 注文発注 | BUY/SELL → 注文執行 | エントリー価格 / SL / TP / 判断理由 |
| 決済 | SL/TP 到達・review-based close・reconciliation | 後述のラベル + PnL + 残高 |
| スキップ | 既存ポジションあり | 方向・確信度 + 理由 |
| HOLD | シグナルなし | 方向予測 + 理由 |
| 価格急変動 | 損失閾値超過 | 損失率・SL までの距離 |
| auto halt 発動 | 上記 [auto halt 条件](#auto-halt-発動条件) | 発動理由 + 復旧手順 |
| reconciliation alert | external position 検知 / volume 異常 | 該当チケット・乖離量 |

### close_reason ラベルマッピング

| close_reason | 表示ラベル | 出所 |
|---|---|---|
| `take_profit` | ✅ TP 到達 | SL/TP check |
| `stop_loss` | 🛑 SL 到達 | SL/TP check |
| `emergency_stop` | ⚠️ 緊急損切り | price_monitor emergency_close |
| `profit_lock` | 🔐 利益確定 (L3 profit_lock) | exit_check Layer 3 |
| `reversal` | 🔁 反転シグナル (L1 reversal) | exit_check Layer 1 |
| `timeout` | ⏰ 保有期間超過 (L2 timeout) | exit_check Layer 2 |
| `server_sl_tp` | 🔄 MT5 サーバー側決済 (reconciliation 検知) | mt5_bridge reconciliation |
| `manual` | 🔒 手動決済 | `?close` / `/close/{pair}` |
| その他 (未知) | ❓ その他 (`<raw>`) | フォールバック |

## REST API

`api.enabled: true` で FastAPI + uvicorn が起動。認証は `X-API-Key` ヘッダー。

```bash
KEY="your_api_secret_key"
HOST="http://localhost:8811"

# 監視系
curl -H "X-API-Key: $KEY" $HOST/status                    # プロセス + halt + サブシステム
curl -H "X-API-Key: $KEY" $HOST/account                   # 残高 + ポジション + MT5 + 乖離
curl -H "X-API-Key: $KEY" $HOST/schedule
curl -H "X-API-Key: $KEY" $HOST/logs?lines=100
curl -H "X-API-Key: $KEY" $HOST/usage

# データ系
curl -H "X-API-Key: $KEY" $HOST/news
curl -H "X-API-Key: $KEY" $HOST/tech
curl -H "X-API-Key: $KEY" $HOST/analyze
curl -H "X-API-Key: $KEY" "$HOST/forecast/USDJPY%3DX"
curl -H "X-API-Key: $KEY" $HOST/feeds

# 取引系
curl -X POST -H "X-API-Key: $KEY" "$HOST/close/USDJPY%3DX"
curl -X POST -H "X-API-Key: $KEY" $HOST/run/trade
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"message":"ドル円の見通しは？"}' $HOST/ask

# 管理系
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"mode":"soft","reason":"maintenance"}' $HOST/admin/halt
curl -X POST -H "X-API-Key: $KEY" $HOST/admin/resume
```

> finance API 側の旧 `/health` は `/status` に統合 (廃止)。MT5 bridge プロセス側の `/health` (`mt5_bridge/server.py`) は別 endpoint で継続利用。

### /status 応答スキーマ (抜粋)

```json
{
  "status": "ok",
  "mode": "live|live_test|paper",
  "live_broker": "mt5|null",
  "started_at": "...",
  "uptime_seconds": 0,
  "now": "...",
  "scheduler": { "jobs_count": 0, "next_run": "..." },
  "halt": {
    "soft_halted": false,
    "auto_triggered": false,
    "reason": "",
    "since": null,
    "triggered_by": "",
    "blocks_new_orders": false,
    "allows_position_management": true
  },
  "llm_circuit_breakers": { "<provider>": { "state": "CLOSED|OPEN|HALF_OPEN", "consecutive_failures": 0 } },
  "price_provider": "...",
  "snapshots": [ { "symbol": "...", "latest_at": "...", "age_minutes": 0 } ],
  "mt5_bridge": {
    "configured": true,
    "reachable": true,
    "mt5_connected": true,
    "dry_run": false,
    "soft_halted": false,
    "is_hard_halted": false,
    "accepts_new_orders": true,
    "reconciliation_skipped_consecutive": 0
  }
}
```

bridge 不通時は `reachable: false, error: ..., reconciliation_skipped_consecutive: N` で短縮 return。

## reviewer ガードと監視機構

### exit_check reviewer ガード

`live` モードで reviewer による active close 実行時、以下の条件で skip:

1. **price source != "mt5"** → 警告ログ + close skip
   - フォールバック価格 (TwelveData / yfinance) で能動 close 判定すると MT5 実勢と乖離するため
2. **soft halt 中 + close_reason != "timeout"** → INFO ログ + close skip
   - 最大保有期間超過 (timeout) は halt と無関係なので例外的に許容

reviewer の **signal 評価、pending protection target記録、SL/TP reconciliation は継続**する (close 実行フェーズだけを抑制)。

### close 通知ラベル明示化

`notify_order_closed` は close_reason ごとに明示分岐 + 未知値は raw 値併記 (前述 [close_reason ラベルマッピング](#close_reason-ラベルマッピング) 参照)。

### shadow reconciliation stale 監視

`state_dir/reconciliation.json` で broker インスタンス横断の連続 skip カウンタを永続化。`halt_state` パターンに従い atomic write + `_get_state_lock` で安全な read-modify-write。

## ディレクトリ構成

```
finance/
├── main.py                         # エントリーポイント・スケジューラ
├── client.py                       # 対話 CLI (REST API 経由)
├── config/
│   ├── settings.yaml               # 運用パラメータ (mode/LLM/trading/schedule)
│   ├── instruments.yaml            # 銘柄 + asset_type
│   ├── news_sources.yaml           # キーワード + フィード
│   ├── hosts.yaml                  # クライアント接続先プロファイル
│   ├── providers/
│   │   ├── mt5.yaml                # MT5 bridge URL / API key
│   │   └── twelvedata.yaml         # TwelveData 設定
│   └── user_notes.md               # ユーザーメモ (LLM プロンプト注入)
├── prompts/                        # LLM プロンプトテンプレート
├── src/
│   ├── config/                     # 設定スキーマ + ローダー
│   ├── cycles/                     # 取引/予測/exit_check サイクル
│   ├── llm/                        # LLM クライアント + サーキットブレーカー
│   ├── analysis/                   # ニュース・テクニカル分析・振り返り・audit
│   ├── signals/                    # シグナル統合・RAG 補正
│   ├── data/                       # OHLCV・指標・相関・スナップショット集約
│   ├── jobs/                       # 収集ジョブ (ニュース/テクニカル/価格監視/経済指標)
│   ├── trading/                    # broker (paper/shadow/mt5_bridge)・SL/TP・ポジション管理
│   ├── rag/                        # ChromaDB・方向別ストア・セマンティック検索
│   ├── api/                        # REST API (FastAPI) — routes/state/server
│   ├── notifications/              # Discord 通知
│   ├── persistence/                # halt_state / balance_snapshot / reconciliation_state / state_store
│   ├── concurrency/                # JobGuard / PriorityJobSlot
│   ├── utils/                      # 時刻ヘルパー (clock.py) 等
│   └── reporting/                  # Rich CLI レポート
├── tests/                          # pytest
├── docs/
│   └── audit/                      # audit レポート出力先
├── data/
│   ├── prices.db                   # SQLite (OHLCV + スナップショット + セッション + 経済指標)
│   ├── state/
│   │   ├── positions.json          # オープンポジション一覧
│   │   ├── trades.json             # クローズ済み取引履歴
│   │   ├── balance.json            # 残高 (deposit / peak / fetched_at)
│   │   ├── halt.json               # halt 状態 (auto/manual/hard 区別)
│   │   ├── reconciliation.json     # bridge skip 連続カウンタ + stale_warned
│   │   ├── mt5_heartbeat.jsonl     # bridge probe 履歴
│   │   ├── shadow_trades.jsonl     # live_test の shadow 取引記録
│   │   └── adaptive_params.yaml    # ペア別 ATR 倍率 (動的更新)
│   ├── shadow_state/               # live_test の LiveTestObserver 別ストア
│   └── rag/                        # ChromaDB (ニュース・振り返り・洞察・方向別・経済指標)
├── mt5_bridge/                     # Windows 側 bridge プロセス (別 venv)
└── logs/
    ├── finance.log                 # 全ログ (ローテーション対応)
    └── activity.log                # 取引・ニュース活動ログ
```

## 状態ファイル

| ファイル | 内容 | 書込み経路 |
|---|---|---|
| `data/prices.db` | OHLCV + スナップショット + 予測 + セッション + HOLD 判定 + 経済指標 | 各収集ジョブ |
| `data/state/positions.json` | オープンポジション | PositionManager (StateStore lock) |
| `data/state/trades.json` | クローズ済み取引履歴 | PositionManager (lock) |
| `data/state/balance.json` | 残高 / deposit / peak / source / fetched_at | balance_snapshot.mutate (lock) |
| `data/state/halt.json` | halt 状態 | halt_state.mutate (lock) |
| `data/state/reconciliation.json` | bridge reconciliation skip カウンタ | reconciliation_state.mutate (lock) |
| `data/state/mt5_heartbeat.jsonl` | bridge probe 履歴 (append-only) | bridge_health_gate |
| `data/state/adaptive_params.yaml` | ペア別 ATR 倍率 | reflector.py |
| `data/rag/` | ChromaDB | news_collector / reflector |
| `config/audit_lessons.md` | audit で承認された改善ルール (LLM プロンプト注入) | audit_reviewer |

`halt_state`、`balance_snapshot`、`reconciliation_state` は共通のロックレジストリ (`_get_state_lock`) を使い、`state_dir` 単位の read-modify-write を直列化する。

## 休場時の動作

MarketStateTracker が市場の開閉状態を管理:

| 状態遷移 | ログ出力 |
|---|---|
| 開場→休場 | `Market CLOSED — pausing until market open` (1 回のみ) |
| 休場→開場 | `Market OPEN — resuming normal operations` (1 回のみ) |
| 休場継続 | 6 時間ごとのハートビート (`Scheduler alive, jobs paused`) |
| 休場中 | 取引判定・価格監視は無音スキップ。ニュース収集は継続 |

## ログプレフィックス

| プレフィックス | 内容 |
|---|---|
| `[COLLECT]` | ニュース・OHLCV 取得・テクニカル分析 |
| `[CORR]` | 銘柄間相関計算 |
| `[AGGREGATE]` | スナップショット指数減衰集約 |
| `[NEWS]` | ニュース分析結果 |
| `[PRICE]` | 価格分析結果 |
| `[SIGNAL]` | シグナルスコア内訳 |
| `[CLOSE]` | SL/TP 到達検出 |
| `[TRADE]` | 注文実行・決済 |
| `[REFLECT]` | 振り返り結果 |
| `[FORECAST]` | 予測サイクル |
| `[REVIEW]` | ポジション再評価 (Reversal Guard / Time Stop) 判定詳細 |
| `[EXIT]` | exit_check サイクルの close 実行 |
| `[MONITOR]` | 価格監視・急変動・トレーリング |
| `[CB/*]` | サーキットブレーカー状態遷移 |
| `[RAG ADJ]` | 方向別 RAG スコア補正 |
| `[ECON]` | 経済指標カレンダー・影響分析 |
| `[AUDIT]` | Performance audit 実行・候補生成・教訓追記 |
| `[BRIDGE_GATE]` | bridge probe / auto halt 発動 |
| `[RECONCILE]` | reconciliation 結果・stale 警告 |
| `[MT5_BRIDGE]` | MT5 bridge 通信 |
| `[HALT]` | halt.json 操作 |
| `[ADMIN]` | manual halt/resume |
| `[TRAIL_REMOTE]` | remote_sl_sync |
