# forecast サイクル・取引サイクル退役 + reflection job 新設 設計

- 日付: 2026-07-18
- ステータス: 設計確定 (実装前)
- 関連: `docs/superpowers/notes/2026-07-04-consolidated-roadmap.md` (forecast 退役 → outlook)、
  technical-llm-omit (0 ベース migration の前例)

## 0. 背景と決定

発注判断は orchestrator (planning / watch / order ループ) に一本化された。
`orchestrator.mode=live` の時点で従来の取引サイクル (`run_trading_cycle`) の発注機能
(Phase 4b) は early return で既に死んでおり (`src/cycles/trading.py:873-893` 相当)、
残っているのは学習・記録系の副作用のみ。forecast サイクルの主要 consumer
(forecast_accuracy_feedback) も取引サイクルのシグナル経路にしか効いていない。

ユーザー決定事項:

1. **恒久退役** — 一時停止フラグではなくコード削除 (退役の第一歩ではなく一気に完全削除)。
2. **orchestrator 継続** — 発注は orchestrator に一本化。
3. **振り返り (決済後 LLM reflection) だけ移設** — 新規独立 job として再設計。
   adaptive params 更新・HOLD レビューは退役。
4. **記録先は OrchestratorStore に一本化** — session_store は退役。
5. **手動 `audit` コマンドは今回削除** — orchestrator 記録ベースの audit は将来別 spec。
6. **既存データテーブルは migration で drop**。

## 1. 調査結果 (設計の根拠)

### 1.1 orchestrator の依存

orchestrator が旧シグナル系から借りているのは `TradeSignal` dataclass と
`_calculate_position_size` のみ (`src/orchestrator/runtime.py:84`)。
`combine_signals` / `rag_adjustment` / `adaptive_store` / `hold_store` /
`session_store` / `forecast` への参照はゼロ。

### 1.2 学習ループの consumer 分析

| 生産物 | 生産者 | consumer | 退役後 |
|---|---|---|---|
| forecast 精度 (`forecasts` reviewed=1) | forecast サイクルのみ | `signal_combiner` accuracy 分岐・weekly_diagnosis・ask・API/views | consumer ごと削除 |
| adaptive params (JSON) | `_finalize_closed_orders` | `_apply_atr_sltp_to_signal` (取引サイクルのみ) | 両方削除 |
| HOLD レビュー (`hold_decisions`) | 取引サイクル Phase 2.5 | RAG カード供給のみ | 削除 |
| session (`trading_sessions`) | `create_session` (live で死コード) / `close_session` | performance_audit・ask・views | 全て削除 |
| directional RAG complete カード | `_finalize_closed_orders` / `_review_hold_decisions` | **news_collector (生存)**・`_adjust_signal_with_rag` (退役) | reflection job が供給継続 |

directional RAG complete カードだけは生きた consumer が残る
(`src/jobs/news_collector.py:135-146` が「過去の取引教訓」として LLM 分析プロンプトに注入)。
これが reflection 移設の主目的の 1 つ。

### 1.3 決済検知の現状と欠陥

現行の振り返りは「取引サイクル自身がそのサイクル内で close したオーダーの戻り値」
(`closed_this_run`) にしか走らない。exit_check サイクルや broker 側 SL/TP 自動執行で
閉じたポジションは**元々振り返り漏れ**している。決済の永続記録は
`trades.json` (StateStore / PositionManager) が正。

### 1.4 テーブル/ファイル実体

- `forecasts` / `hold_decisions` — `src/data/analysis_store.py` 内 (prices.db 同居)
- `trading_sessions` — `src/data/session_store.py` (prices.db 同居)
- adaptive params — SQLite でなく **state_dir 配下の JSON ファイル**
  (`src/persistence/adaptive_params_store.py`)
- `src/trading_cycle.py` は後方互換 shim (cycles/ からの re-export)。
  main.py / cli.py / tui.py / api / views が経由。

## 2. 削除スコープ

### 2.1 forecast 系 (全削除)

- `src/cycles/forecast.py` (`run_forecast_cycle` / `forecast_cycle`)
- `ForecastStore` / `_ForecastRecord` (`src/data/analysis_store.py:320-`)
- `src/signals/accuracy_tracker.py` (全体)
- `signal_combiner.py` の forecast accuracy 分岐 (概ね 101-144 行)。
  **`TradeSignal` / `_calculate_position_size` は orchestrator が使うため残す**
- `directional_writer.py` の `record_forecast_entry` / `record_forecast_review`
- config キー: `forecast_accuracy_feedback` (schema + loader)、
  `forecast_review_interval_hours`、`forecast_start_hour`、
  `forecast_min_combined_score`、`forecast_significance_atr_ratio`、
  `rag_adjustment_forecast_multiplier`
- 表示・導線: weekly_diagnosis の accuracy セクション、
  `ask_context_builder` の forecast_accuracy (プロンプト変数 `prompts/ask_user.j2` 含む)、
  API `/forecast` (`src/api/routes/data.py`)、views/CLI/TUI の forecast ビュー、
  main.py のジョブ登録 + JobGuard("forecast") + Schedule 表示行、
  notifier の `accuracy_gate` フラグ、`_SOURCE_LABELS` の "forecast"

### 2.2 取引サイクル系 (全削除)

- `src/cycles/trading.py` の `run_trading_cycle` / `trading_cycle` と専用フェーズ群:
  `_phase_analyze_pairs`、`_phase_execute_signals`、`_execute_one_signal`、
  `_review_hold_decisions`、`_phase_close_sl_tp`、`_phase_review_open_positions`、
  `_finalize_closed_orders` (reflection job へ再設計移設)、
  `_adjust_signal_with_rag`、`_apply_atr_sltp_to_signal`、`_process_pair` ほか
- exit_check / orchestrator / views が import する共有ヘルパ
  (`_summarize_pair`、`_build_trading_runtime` 等) は削除前に依存を実測し、
  必要なものは `src/cycles/_helpers.py` 等へ移動して温存
- `HoldDecisionStore` / `_HoldDecisionRecord` (`src/data/analysis_store.py:448-`)
- `src/persistence/adaptive_params_store.py` (全体)
- `src/data/session_store.py` (全体)
- `src/analysis/performance_audit.py` + CLI `audit` コマンド
- `src/signals/rag_adjustment.py` (consumer は取引サイクルのみ)
- CLI / API (`src/api/routes/trading.py` の手動実行) / TUI の取引サイクル実行導線
- main.py: ジョブ登録・JobGuard・Schedule 表示・`run_times` を参照する分岐
  (forecast 時刻フィルタ含む)
- `src/trading_cycle.py` shim の該当 re-export 整理
  (exit_check 系 re-export は残す or 呼び出し元を `src/cycles/` 直接 import に更新)

### 2.3 残すもの (明示)

- exit_check サイクル (SL/TP 監視、LLM なし)
- price_monitor / protection (tick_migration_stage 系)
- news 収集 (deep fetch / directional RAG query 含む)
- technical 収集 (cadence driver)
- econ calendar / econ impact
- weekly_diagnosis (accuracy セクション抜きで継続)
- orchestrator 全系 (planning / watch / order / market_state / 承認ゲート)
- directional RAG 本体 (`fx_reflections_bullish/bearish`) — reflection job が書き、
  news_collector が読む
- `TradeSignal` / `_calculate_position_size` (orchestrator 依存)
- halt_state / balance_snapshot / StateStore / PositionManager (exit_check・orchestrator が使用)

## 3. reflection job (新規 `src/cycles/reflection.py`)

### 3.1 目的

決済済みトレードの LLM 振り返りを生成し、
(a) OrchestratorStore の `reflections` テーブルに永続化、
(b) directional RAG に `phase="complete"` カードとして upsert して
news_collector への教訓供給を維持する。

### 3.2 検知 (全決済経路カバー)

```
未振り返り集合 = trades.json の closed trades
              − reflections テーブルに記録済みの order_id
```

- trades.json (StateStore) を読む。取引サイクル・exit_check・broker 側 SL/TP 執行・
  手動決済のいずれ経由でも trades.json に落ちる限り拾える。
  現行の「exit_check 決済は振り返り漏れ」も解消される。
- 二重処理防止は `reflections.order_id` の存在チェック (PK/UNIQUE) で行う。
- 起動直後の大量バックログを避けるため、1 回の実行での処理件数に上限を設ける
  (10 件固定、古い順)。残りは次回実行で処理する。

### 3.3 文脈組み立て

- `order_id` → OrchestratorStore `order_intent` → `plan_id` → plan の
  planner reasoning (`get_latest_plan_create_reasoning`) を entry 分析文脈として
  LLM プロンプトに渡す。
- orchestrator 経由でない決済 (旧取引サイクル発注の残ポジ・手動) は plan なし →
  文脈なしで振り返りを実行する (skip しない)。
- 旧 `_finalize_closed_orders` が渡していた session 由来の文脈
  (analysis_summary / macro_context / SLTP 比較 / adaptive param 履歴) は退役に伴い消滅。
  プロンプトは plan reasoning + 決済事実 (pair / 方向 / entry / close / 損益 / close_reason)
  ベースに簡素化する。

### 3.4 出力

1. **`reflections` テーブル** (OrchestratorStore `_migrate()` に CREATE 追加):
   - `order_id` TEXT PRIMARY KEY
   - `plan_id` INTEGER NULL (orchestrator 経由でない決済は NULL)
   - `pair` TEXT NOT NULL
   - `close_reason` TEXT
   - `realized_pnl` REAL
   - `reflection_text` TEXT NOT NULL
   - `created_at` TEXT NOT NULL (db_now)
2. **directional RAG**: 既存 `record_trade_complete` を流用して
   `phase="complete"` カードを upsert。

保持方針: reflections は軽量テキストのため prune しない (plan 系と同じ長期保持)。

### 3.5 失敗時挙動

- LLM 失敗・RAG 失敗: その order は記録せず warning ログ → 次回実行で自然リトライ。
- **`reflections` への INSERT は LLM 成功 + RAG upsert 成功の後** (途中失敗を
  「処理済み」にしない)。RAG upsert が成功し INSERT が失敗した場合は次回
  RAG 側が upsert (冪等) で上書きされるだけで害はない。
- trades.json 読み込み失敗: job 全体を warning で skip (次回リトライ)。

### 3.6 スケジュール

- 毎時実行 (exit_check と同じ毎時系だが独立 job)。
- LLM slot 経由 (`_run_with_slot`) で他 LLM job と排他。JobGuard 付き。
- LLM は既存 `config.llm.reflection` 設定 (モデル / temperature) を再利用。
  新規 config キーは追加しない (interval は固定毎時)。

## 4. migration (0 ベース手順)

technical-llm-omit のデプロイと同梱可能な手順として:

- DROP TABLE: `forecasts`、`hold_decisions`、`trading_sessions`
- ファイル削除: state_dir の adaptive params JSON
- CREATE: `reflections` (OrchestratorStore `_migrate()` が起動時に自動作成するため
  手動手順は不要)

migration スクリプトは冪等 (`DROP TABLE IF EXISTS`) とし、実行前に DB バックアップを
取る手順を明記する (rsync 事故の教訓に従い、バックアップ→実行の順を厳守)。

## 5. config 変更

- 削除キーは §2.1 の通り (schema dataclass + loader + settings.yaml.example)。
- loader は未知キーを黙殺するため、実 config (settings.yaml) に旧キーが残っても
  起動は壊れない。ただし黙殺は「消し忘れに気付けない」ことと同義なので、
  デプロイ手順に**実 config からの旧キー削除**を明記する (discord_bot config 再構成で
  移行漏れした前例あり)。
- 新設キー: なし。

## 6. テスト戦略

- **reflection job**: TDD で新規作成。
  - 検知差分 (closed − 記録済み = 未処理集合)
  - 処理件数上限と古い順
  - plan 文脈あり / なし (plan_id NULL) の両経路
  - LLM 失敗 → 記録なし → 次回再試行
  - RAG 成功 + INSERT 失敗 → 次回再処理で冪等
  - 二重処理防止 (記録済み order_id は再処理しない)
- **reflections テーブル**: OrchestratorStore migration テスト既存パターンに追加。
- **削除系**: 削除ごとに (a) 参照残りゼロ (grep + import 確認)、
  (b) 影響ファイルの per-file pytest green。
- **既存テストの整理**: 削除対象 (trading cycle / forecast / accuracy / rag_adjustment /
  session / hold / adaptive / audit) のテストは削除。共有部 (TradeSignal /
  _calculate_position_size / exit_check / _helpers) のテストは残し green を確認。
- 回帰基準は per-file green (full suite は既知の順序依存フレークあり —
  `finance_fullsuite_order_flake` 参照)。

## 7. スコープ外 (明示)

- forecast の後継 (outlook) の設計 — roadmap の別項目。
- orchestrator 記録ベースの audit 再設計 — 将来別 spec。
- exit_check の LLM 化・orchestrator への統合 — 現状維持。
- adaptive params に相当する SL/TP 自己調整の orchestrator 版 —
  planner + risk_gate が SL/TP を決める現行設計のままとし、必要になったら別 spec。
- reflection の内容を planner にフィードバックする仕組み (RAG 経由の間接供給のみ)。

## 8. デプロイ注意

- 本変更は stick (Live) / Fiosracht (paper) 両方に影響する。
  デプロイ時は §4 の migration + §5 の実 config 掃除をセットで実施。
- technical-llm-omit (未マージ、0 ベース migration 必須) とマージ順序・デプロイ順序を
  合わせて計画すること。
- 停止対象 job が消えることで Schedule 表示・health エンドポイントの job 一覧も変わる。
  discord_bot 側の `/health` 表示に取引サイクル/forecast 依存があれば追従が必要
  (実装時に確認)。
