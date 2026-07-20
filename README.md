# FX Trading Bot (LLM + RAG)

ニュース RAG × LLM × ルールベース指標を統合した FX スウィングトレード自動売買システム。ペーパー / シャドウ / 実弾の 3 モードを切替可能。実弾モードでは MT5 ブリッジ経由で外部ブローカーへ発注。

## 概要

| 項目 | 内容 |
|---|---|
| 取引モード | `paper` (シミュレーション) / `live_test` (paper primary + MT5 shadow record) / `live` (MT5 経由本取引) |
| ライブブローカー | MT5 ターミナル (mt5_bridge 経由) |
| 取引スタイル | スウィング (3〜10 日想定) |
| 価格データ | MT5 ティック (live) / Twelve Data (リアルタイム) / yfinance (フォールバック) |
| ニュース | RSS / Feedly API |
| 分析エンジン | Claude / OpenAI / Gemini / Ollama (役割ごと個別設定可) |
| RAG | ChromaDB + nomic-embed-text |
| 言語 | Python 3.12 / uv |
| テスト | pytest |

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│  ニュース収集 (30 分間隔・LLM)                                │
│  RSS/Feedly → カテゴリ別バッチ分析 → ChromaDB                 │
├─────────────────────────────────────────────────────────────┤
│  テクニカル分析 (毎時 :00・LLM)                                │
│  OHLCV → 指標 → ルールベーススコア → スナップショット蓄積       │
├─────────────────────────────────────────────────────────────┤
│  orchestrator (発注主体・常駐ループ)                           │
│  planning → watch → 発注タイミング判断 → 承認ゲート → 発注     │
├─────────────────────────────────────────────────────────────┤
│  決済振り返り (毎時 :00・LLM)                                  │
│  決済検知 → LLM 振り返り → reflections + directional RAG       │
├─────────────────────────────────────────────────────────────┤
│  exit_check (毎時 :00・LLM なし)                              │
│  SL/TP 確認 + reconciliation + Layer 1-3 再評価 (オプション)   │
├─────────────────────────────────────────────────────────────┤
│  価格監視 (10 分間隔)                                         │
│  急変動通知 → 緊急損切り (任意) → Layer 4 トレーリング        │
└─────────────────────────────────────────────────────────────┘
```

### 関連プロセス

| プロセス | 役割 |
|---|---|
| `mt5_bridge/` (Windows 側) | MT5 ターミナルと HTTP 通信する別プロセス。`/health` `/positions` `/admin/halt` `/admin/resume` 等を提供 |
| `discord_bot/` (任意) | `?status` `?account` `?close` `?halt` `?resume` 等の運用コマンドを Discord 経由で提供 |

## セットアップ

### 前提

Python 3.12+、[uv](https://docs.astral.sh/uv/)、Discord Webhook (任意)、MT5 ターミナル + bridge (live モード時)

### インストール

```bash
uv sync
cp .env.example .env             # API キー・Webhook URL 等
cp config/settings.yaml.example config/settings.yaml
cp config/llm.yaml.example config/llm.yaml
cp config/strategy.yaml.example config/strategy.yaml
cp config/instruments.yaml.example config/instruments.yaml
cp config/news_sources.yaml.example config/news_sources.yaml
cp config/hosts.yaml.example config/hosts.yaml
```

LLM プロバイダによって追加セットアップ:
- Claude / OpenAI / Gemini: API キーを `.env` に
- Ollama: `ollama pull llama3.1:8b && ollama pull nomic-embed-text`

### 実行

```bash
uv run python main.py                   # 通常起動 (フォアグラウンド)
uv run python main.py --daemon          # REST API のみ (スケジューラ無効)
uv run python main.py --skip-news       # 起動時ニュース取得スキップ
uv run python main.py --skip-tech       # 起動時テクニカル収集スキップ
```

## 運用モード

| mode | primary trading | MT5 連携 | 用途 |
|---|---|---|---|
| `paper` | 内部 PositionManager (TwelveData/yfinance 価格) | なし | シミュレーション・初期検証 |
| `live_test` | 内部 PositionManager (TwelveData) | shadow observer のみ (記録専用) | 本番前のドライラン |
| `live` | MT5 経由実発注 | full integration | 実弾運用 |

`config/settings.yaml` の `mode`、`paper_provider`、`live_broker` の組み合わせで決定。

## 設定ファイル

```
config/
  settings.yaml          — 実行基盤 (mode / provider 選択 / timezone / logging / api / 通知)
  llm.yaml               — LLM / agents / embedding
  strategy.yaml          — 売買戦略 (trading / price_monitor / schedule / analysis /
                           news_collection / economic_calendar / orchestrator / rag)
  instruments.yaml       — 銘柄定義 + asset_type / pip_value
  news_sources.yaml      — キーワード + RSS フィード + Feedly
  hosts.yaml             — REST API クライアントの接続先プロファイル
  providers/mt5.yaml     — MT5 bridge URL / API key / 認証
  providers/twelvedata.yaml — TwelveData API key / 日次上限
  user_notes.md          — LLM プロンプトに注入する自由記述メモ
```

各ファイルは `.example` をコピーして利用。

`settings.yaml` / `llm.yaml` / `strategy.yaml` は top-level ブロック単位でマージされる
(config migration 2026-07-20)。同じブロックを複数ファイルに書くと起動時 `ConfigError`。
`llm.yaml` / `strategy.yaml` は必須で、欠損・空でも `ConfigError` (既定値への無警告な
転落を防ぐため)。タイムゾーンは `settings.yaml` の top-level `timezone` が唯一の設定箇所
(旧 `schedule.timezone` / `news_collection.timezone` / `economic_calendar.fetch_timezone`
は廃止)。

## CLI コマンド (`uv run python client.py` から対話)

| コマンド | 略記 | 内容 |
|---|---|---|
| `status` | `s` | 運用状態 (プロセス + halt + サブシステム健全性) |
| `account` | `acc` | 残高 / 損益 / ポジション / MT5 実残高 / 乖離 |
| `run news` | `run n` | ニュース収集を即時実行 |
| `run tech` | `run t` | テクニカル分析を即時実行 |
| `run analyze` | `run a` | 総合シグナル表示 |
| `ask <質問>` | | セマンティック検索で回答 |
| `close <pair>` | | ポジション手動決済 |
| `halt soft\|hard <reason>` | | 取引停止 (soft = 新規停止 / hard = bridge 全 close) |
| `resume` | | soft halt 解除 |
| `logs [N]` | | activity.log 末尾 N 行 |
| `usage` | | LLM 使用量 / CB 状態 |
| `schedule` | | スケジュール一覧 |
| `feeds` | | RSS フィード疎通確認 |
| `tech` | | キャッシュ済みテクニカルスナップショット |
| `hosts` / `use <name>` | | 接続先ホスト切替 |

## REST API

`api.enabled: true` で FastAPI サーバーが起動。認証は `X-API-Key` ヘッダー。

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/status` | 運用状態 (プロセス + halt + サブシステム健全性) |
| `GET` | `/account` | 残高・ポジション・MT5 実残高・乖離 |
| `GET` | `/logs` | activity.log 末尾 |
| `GET` | `/usage` | LLM プロバイダ別使用量 / CB 状態 |
| `GET` | `/schedule` | スケジュール情報 |
| `GET` | `/news` | ニュースセンチメント |
| `GET` | `/tech` | テクニカルスナップショット (latest_collect + latest_ok) |
| `GET` | `/analyze` | 総合シグナル |
| `GET` | `/feeds` | RSS フィード疎通確認 |
| `POST` | `/close/{pair}` | ポジション手動決済 |
| `POST` | `/ask` | セマンティック検索質問 |
| `POST` | `/admin/halt` | soft / hard halt 発動 (mt5_bridge プロキシ) |
| `POST` | `/admin/resume` | soft halt 解除 (bridge accept 状態確認込み) |

## 主要な安全機構

### auto halt (自動停止)

下記いずれかで auto soft halt 発動 (新規発注停止、既存ポジション管理は継続):

- bridge `/health` 連続失敗 (1 min retry 含む 2 回)
- 注文連続 REJECT (閾値超過)
- balance divergence の閾値超過 (一定時間継続)
- balance.json 破損
- collect_status sentinel 連続失敗

### exit_check reviewer ガード

`live` モードで Layer 1-3 reviewer による能動 close を行う際、以下を満たさないと close をスキップ:
- price source が `"mt5"` (フォールバック価格で close 判定しない)
- soft halt 中は `timeout` 以外の close を skip

### shadow reconciliation stale 監視

`Mt5BridgeBrokerAdapter` の reconciliation skip 連続回数を `state_dir/reconciliation.json` に永続化。
閾値到達で警告ログ、`/status` の `mt5_bridge.reconciliation_skipped_consecutive` で監視可能。

### close 通知ラベル

close_reason ごとに明示的なラベル (Discord 通知):
- `take_profit` → ✅ TP 到達
- `stop_loss` → 🛑 SL 到達
- `emergency_stop` → ⚠️ 緊急損切り
- `profit_lock` → 🔐 利益確定 (L3 profit_lock)
- `reversal` → 🔁 反転シグナル (L1 reversal)
- `timeout` → ⏰ 保有期間超過 (L2 timeout)
- `server_sl_tp` → 🔄 MT5 サーバー側決済 (reconciliation 検知)
- `manual` → 🔒 手動決済

## 詳細ドキュメント

アーキテクチャ詳細・設定リファレンス・ロジック解説は [DETAIL.md](DETAIL.md) を参照。

---

## 注意事項

- 土日 (FX 市場休場) は価格監視・テクニカル分析を自動スキップ (ハートビートのみ)。
  決済振り返りは休場中も動く (決済は休場を跨いで残るため)
- yfinance の価格データには最大 15 分の遅延あり (live モードでは MT5 ティックを使用)
- RAG の効果はデータ蓄積とともに向上。運用開始直後は振り返りコンテキストがない状態で動作

## 免責事項

本ソフトウェアは教育・研究目的で提供されるものであり、投資助言・売買推奨を目的としたものではありません。

- 本システムの利用によって生じた**いかなる損害・損失**（金銭的損失を含む）についても、開発者は一切の責任を負いません
- LLM・テクニカル指標による分析結果は将来の相場を保証するものではなく、**投資判断はすべて自己責任**で行ってください
- `mode: live` での実弾運用は、価格データ・ニュースデータ・MT5 ブリッジ・LLM サービスのいずれかの不調により予期せぬ損失を生じる可能性があります
- 価格データ・ニュースデータの正確性・完全性・適時性について、開発者は保証しません
