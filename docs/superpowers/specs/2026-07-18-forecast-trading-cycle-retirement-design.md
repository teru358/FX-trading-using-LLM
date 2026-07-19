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
| adaptive params (YAML) | `_finalize_closed_orders` | `_apply_atr_sltp_to_signal` (取引サイクルのみ) | 両方削除 |
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
- adaptive params — SQLite でなく **state_dir 配下の YAML ファイル (adaptive_params.yaml)**
  (`src/persistence/adaptive_params_store.py`)
- `src/trading_cycle.py` は後方互換 shim (cycles/ からの re-export)。
  main.py / cli.py / tui.py / api / views が経由。

## 1.5 起動要件の変更 (fail-fast) — レビュー High-1 対応

現行 main.py は「orchestrator が立ち上がらなくても本体は継続する」設計
(段階導入期の guard)。旧サイクル削除後にこの guard が残ると、
`orchestrator.enabled=false`・bootstrap 失敗・runtime 起動失敗のいずれでも
**発注経路ゼロのまま無音で運転を続ける**。

要件:

- `orchestrator.enabled=false` → **起動中止** (明示エラーで exit)。
  データ収集専用運転が必要になったら明示フラグを別途設計する (今回スコープ外)。
- `build_orchestrator_runtime()` が None を返す / 例外 / runtime 起動失敗 →
  **起動中止**。現行の try/except 継続 guard は撤去する。
- `orchestrator.mode=shadow` は**許容** (Fiosracht の段階検証運用があるため
  live 必須にはしない)。ただし起動時に「発注は行われない (shadow)」を
  warning ログ + Schedule 表示に明示する。
- **起動順序**: 現行 main は API 起動 → scheduler 起動 → orchestrator 構築の順
  (main.py:504 / 543 / 554)。このままでは orchestrator 構築失敗までの短時間に
  API が ready を返し定期 job が動き得るため、**orchestrator の構築 + 起動可能性
  検証を API・scheduler 起動より前に移す**。
- **pair 集合の整合**: bootstrap 解決後の orchestrator 対象 pair 集合が
  tradeable 全体と一致しない場合、`mode=live` では対象外 pair の発注経路が
  消えるため**起動中止**とする (subset 運用は tradeable 側の設定で表現する。
  許可フラグは設けない)。`mode=shadow` では warning のみ。

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
- `src/analysis/forecaster.py` (forecast サイクルの LLM なし予測生成本体)
  - **訂正 (Task 6 実施時判明)**: 実際には `build_forecast_review` /
    `build_forecast_review_summary` / `build_hold_review` の 3 関数が同居しており、
    `build_hold_review` は取引サイクル Phase 2.5 の hold review (退役対象) の部品だった。
    ファイル削除により trading.py の lazy import が破損したため、hold review 側は
    前倒しで削除した (plan Task 6 の注記参照)
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
- `src/analysis/performance_audit.py` + CLI `audit` コマンド、
  `src/analysis/audit_post_hoc.py`・`src/analysis/audit_report.py` (caller sweep で確認の上)
- `src/signals/rag_adjustment.py` (consumer は取引サイクルのみ)
- `directional_writer.py` の `record_trade_entry` (`phase="entry"` カード。
  唯一の caller が退役する Phase 4b)
- CLI / API (`src/api/routes/trading.py` の手動実行) / TUI の取引サイクル実行導線
- main.py: ジョブ登録・JobGuard・Schedule 表示・`run_times` を参照する分岐
  (forecast 時刻フィルタ含む)
- config: `schedule.run_times` (schema + loader + example)。
  `price_provider.estimate_daily_requests()` の run_times 加算項も除去
- config (caller sweep で確認の上): `rag_adjustment_enabled` /
  `rag_adjustment_max` / `rag_adjustment_min_hits` / `rag_adjustment_search_top_n` /
  `rag_adjustment_same_weight` / `rag_adjustment_opposite_weight` /
  `rag_adjustment_trade_multiplier` / `rag_adjustment_hold_multiplier`、
  `atr_timeframe`・`sl_atr_mult_default/min/max`・`tp_atr_mult_default/min/max`
  (adaptive/取引サイクル専用なら削除。`_helpers.py` の ATR 計算を exit_check /
  orchestrator が使う場合は該当キーのみ残す)
- notifier: `notify_on_cycle_summary` / `notify_on_order_open` /
  `notify_on_signal_skipped` と対応イベント (`CycleSummaryEvent` 等)・通知処理
  (発火元が取引サイクルのみであることを caller sweep で確認の上)
- `src/trading_cycle.py` shim の該当 re-export 整理
  (exit_check 系 re-export は残す or 呼び出し元を `src/cycles/` 直接 import に更新)

### 2.2b discord_bot 側 (別リポジトリ、同時展開必須)

- `cogs/finance/client.py` の `forecast()` / `run_trade()` メソッド
- `cogs/finance/finance_cog.py` の `finance_forecast` / `finance_run_trade`
  tool schema・コマンド対応表・`_forecast_embeds` 等の表示処理
- finance API から `/forecast` / `/run/trade` が消えるため、
  **finance と discord_bot は同時デプロイ** (§8)

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
- 再処理制御は `reflections.status` で行う: **`done` / `dead` は再処理しない。
  `retry` は `next_retry_at` 到来後にのみ再処理する** (§3.2b)。

### 3.2b retry 管理と飢餓防止 (再レビュー High-2 対応)

「古い順 N 件 + 成功するまで未記録」だけでは、恒久失敗レコード (例: 現在の
instrument 設定に存在しない旧銘柄 — 現行実装は skip している) が先頭を占有し、
新規決済の振り返りが飢餓する。対応:

- `reflections` に retry 管理カラムを持たせる:
  `status` (`done` | `retry` | `dead`) / `attempt_count` / `last_error` /
  `next_retry_at`。
- 検知集合 = closed trades − (`done` ∪ `dead` ∪ `next_retry_at` 未到来の `retry`)。
- 失敗時: `retry` として upsert し `attempt_count` を増分、
  `next_retry_at` = 指数 backoff (1h → 2h → 4h → 8h — 5 回目の失敗は dead になるため 4 段で全段使われる)。
  **5 回失敗で `dead` (dead-letter) に落とし warning ログ**。以後検知対象外。
- pair が現在の instrument 設定に存在しない order は**即 `dead`**
  (last_error に理由記録)。
- 成功時のみ `status="done"` (INSERT/upsert は LLM + RAG 成功後 — §3.5 の
  不変条件は status="done" に対して適用)。
- **1 回の実行枠は 10 件** (分類定義 — 再レビュー Medium-3 対応):
  1. **未試行 (attempt_count=0) を新しい順に最大 2 件** — 直近決済の振り返りを
     初回移行バックログより優先する。
  2. **残りの eligible (未試行の残り + next_retry_at 到来済み retry) を
     古い順に最大 8 件** (backfill)。
  片方が枠未満なら他方に融通。watermark は持たない (順序規則だけで一意に決まる)。

### 3.3 文脈組み立て

- `order_id` → OrchestratorStore `order_intent` → `plan_id` → plan の
  planner reasoning (`get_latest_plan_create_reasoning`) を entry 分析文脈として
  LLM プロンプトに渡す。
- `order_intents.order_id` (broker order id) からの逆引き API
  `get_order_intent_by_order_id(order_id)` と index
  `ix_order_intents_order_id` を OrchestratorStore に追加する
  (現行 API は plan_id キーのみ)。
- orchestrator 経由でない決済 (旧取引サイクル発注の残ポジ・手動) は plan なし →
  文脈なしで振り返りを実行する (skip しない)。
- 旧 `_finalize_closed_orders` が渡していた session 由来の文脈
  (analysis_summary / macro_context / SLTP 比較 / adaptive param 履歴) は退役に伴い消滅。
  プロンプトは plan reasoning + 決済事実 (pair / 方向 / entry / close / 損益 / close_reason)
  ベースに簡素化する。

### 3.4 出力

1. **`reflections` テーブル** (ORM model `_Reflection` を追加し、既存パターン通り
   `_get_engine()` の `metadata.create_all()` で自動作成する。`_migrate()` の
   生 SQL は既存テーブルへの ALTER 専用であり使わない):
   - `order_id` TEXT PRIMARY KEY
   - `plan_id` INTEGER NULL (orchestrator 経由でない決済は NULL)
   - `pair` TEXT NOT NULL
   - `close_reason` TEXT
   - `realized_pnl` REAL
   - `reflection_text` TEXT NULL (`done` 以外は NULL 可)
   - `was_directionally_correct` BOOLEAN NULL (`done` 時は必須 — §3.5b の機械判定値)
   - `status` TEXT NOT NULL (`done` | `retry` | `dead` — §3.2b)
   - `attempt_count` INTEGER NOT NULL DEFAULT 0
   - `last_error` TEXT NULL
   - `next_retry_at` DATETIME NULL
   - `created_at` / `updated_at` DATETIME NOT NULL (db_now)
2. **directional RAG**: 既存 `record_trade_complete` を流用して
   `phase="complete"` カードを upsert (`session_type="trade"`)。

保持方針: reflections は軽量テキストのため prune しない (plan 系と同じ長期保持)。

### 3.4b directional RAG の検索 filter と既存カード掃除 (レビュー Medium-4 対応)

news_collector の教訓検索は現状 filter なしで全カードを対象にするため、
退役後も既存 forecast / HOLD / trade-entry カードが「過去の取引教訓」として
注入され続ける。対応:

- `DirectionalStore.query()` を複合 filter 対応に拡張し
  (`where={"$and": [{"session_type": "trade"}, {"phase": "complete"}]}`)、
  news_collector の検索を **`session_type="trade" AND phase="complete"`** に限定する。
- migration (§4) で既存の `session_type in ("forecast", "hold")` カードと
  `phase="entry"` カードを directional collections から削除する
  (今後の生産者も消えるため残す意味がない)。

### 3.5 失敗時挙動 (strict 化 — レビュー High-2 対応)

現行 API は失敗を握って正常形を返すため「未記録なら次回リトライ」が成立しない:
`generate_close_reflection()` は LLM 例外を捕捉して factual fallback を返し
(`src/analysis/reflector.py:176` 付近)、`record_trade_complete()` も RAG 例外を
捕捉して正常終了する (`src/rag/directional_writer.py:92` 付近)。

対応 — 旧 caller (`_finalize_closed_orders`) は全削除されるため、互換維持は不要:

- `generate_close_reflection()` の **fallback 機構自体を削除**し、LLM 失敗は
  例外を伝搬する (呼び出し側 = reflection job が唯一の caller になる)。
- `record_trade_complete()` も同様に RAG 失敗を例外伝搬に変更する。
- reflection job 側: 1 件の処理で例外が出たらその order を `retry` として記録し
  (attempt_count 増分 + backoff、§3.2b) warning ログ → next_retry_at 到来後に再試行。
- **`status="done"` の記録は LLM 成功 + RAG upsert 成功の後** (途中失敗を
  「処理済み」にしない)。RAG upsert が成功し `done` 記録が失敗した場合は次回
  RAG 側が upsert (冪等) で上書きされるだけで害はない。
- trades.json 読み込み失敗: job 全体を warning で skip (次回リトライ)。

### 3.5b LLM 出力の検証と方向正誤の判定基準 (再レビュー Medium-4 対応)

- **schema validation**: LLM 応答 JSON の必須キー (`outcome_summary` / `lesson` /
  `was_directionally_correct`) が欠落・型不正なら**失敗扱い** (default へ落とさない)
  → §3.2b の retry 管理に乗せる。
- **方向正誤は機械判定を正とする**: 現行 fallback の
  `close_reason == "take_profit"` 基準は trailing SL や manual close の利益決済を
  誤判定する。判定は**価格方向** — buy: `close_price > entry_price`、
  sell: `close_price < entry_price`。LLM の `was_directionally_correct` 申告は
  叙述の整合確認のみに使い、カード/DB に記録する正誤は機械判定を採用する。
- RAG カードの win/loss 表記は既存 directional_writer 準拠
  (`realized_pnl > 0`) を維持する (方向正誤とは別軸)。

### 3.6 スケジュールと LLM 干渉制御 (レビュー High-3 対応)

- 毎時実行 (exit_check と同じ毎時系だが独立 job)。
- **実行形 (再レビュー High-1 対応)** — `_run_with_slot()` は**使わない**
  (別スレッド起動して即 return するため、JobGuard 配下で呼ぶと実処理中に
  guard が解除される)。次の構成とする:
  1. **JobGuard 配下の controller** を `schedule` に登録する
     (`_run_with_guard, _guards["reflection"], controller`)。
     controller は guard スレッド内で同期実行され、完了まで guard を保持する。
  2. controller が §3.2b の検知・優先度付けで処理対象を列挙する。
  3. 各 1 件を **`_llm_slot.try_run_scheduled(process_one, order)` で同期実行**
     (`try_run_scheduled` は呼び出しスレッドで fn を実行し、slot busy なら
     False で skip する — `priority_job_slot.py:70`)。
  4. **slot busy (False 返り)・planning 実行中・`_llm_slot.waiting_user_job`
     が True のいずれかで、その時点で controller を終了**し残りを次回に回す
     (`waiting_user_job` は slot に既存の待機ユーザー処理フラグ —
     `priority_job_slot.py:52`。1 件ごとに slot を解放しても controller が
     即再取得すると最大 10 回連続占有になるため、各件の前に確認する)。
- planner との干渉は以下で最小化する (planner 側コードは変更しない):
  1. **1 件単位の逐次処理** — 上記の通り slot の取得/解放を 1 件ごとに行い、
     まとめて長時間占有しない。
  2. **planning 実行中は譲る** — 各件の処理前に OrchestratorStore の
     `agent_runs` を照会し、実行中の planning run が存在すれば残りを
     次回実行に回す (OrchestratorStore に照会 API
     `has_running_planning_run()` を追加。planner pipeline への semaphore
     差し込みはスコープ拡大と planning レイテンシリスクがあるため不採用。
     プロセス横断の LLM 調停層が必要になったら別 spec)。
     **判定条件と dangling 対策 (再レビュー High-1 対応)**:
     - 実行中判定は **`trigger_type='planning_cycle' AND finished_at IS NULL`**
       (`start_run()` は開始時から `status="ok"` のため status では判定できない —
       `orchestrator_store.py:414` / 実値は `runtime.py:254`)。
     - **stale 除外**: `started_at` が最大 planning 時間 (10 分) より古い未完了 run
       は実行中と見なさない (プロセス異常終了の残骸で reflection が永久に
       譲り続けるのを防ぐ)。
     - **起動時回収**: orchestrator 起動時に `finished_at IS NULL` の dangling run を
       `status="failed"` (error_type="dangling") で finish する。
     - この DB 照会は**競合を完全排除しない best effort** (照会直後に planning が
       始まる窓は残る)。正確性は LLM server 側のキューイングが担保し、本判定は
       レイテンシ干渉の低減のみを目的とする。
  3. LLM server (llama.cpp) は同時リクエストをキュー処理するため、同時になっても
     出力の正確性には影響しない。ただし待ち時間増により client 側 timeout や
     circuit breaker が発火し得る — その場合も §3.2b の retry 管理で回収される
     (運用上の仮定であり、無害の保証ではない)。
- LLM は既存 `config.llm.reflection` 設定 (モデル / temperature) を再利用。
  新規 config キーは追加しない (interval は固定毎時)。

## 4. migration (0 ベース手順)

technical-llm-omit のデプロイと同梱可能な手順として:

- DROP TABLE: `forecasts`、`hold_decisions`、`trading_sessions`
- ファイル削除: state_dir の adaptive params YAML (adaptive_params.yaml)
- directional RAG (ChromaDB): `session_type in ("forecast", "hold")` カードと
  `phase="entry"` カードを削除 (§3.4b)
- CREATE: `reflections` (ORM model 追加により起動時 `metadata.create_all()` で
  自動作成。手動手順は不要)

migration スクリプトは冪等 (`DROP TABLE IF EXISTS`、ChromaDB 削除も再実行安全) とし、
実行前に **DB・ChromaDB データディレクトリ (directional collections を含む
`data/` 配下の RAG 永続化先)・state_dir (削除対象の adaptive params YAML (adaptive_params.yaml) を含む)
をシステム停止中にバックアップ**する手順を明記する
(rsync 事故の教訓に従い、バックアップ→実行の順を厳守。rollback は
このバックアップ + `git revert` で行う)。

## 5. config 変更

- 削除キーは §2.1 の通り (schema dataclass + loader + settings.yaml.example)。
- loader は未知キーを黙殺するため、実 config (settings.yaml) に旧キーが残っても
  起動は壊れない。ただし黙殺は「消し忘れに気付けない」ことと同義なので、
  デプロイ手順に**実 config からの旧キー削除**を明記する (discord_bot config 再構成で
  移行漏れした前例あり)。
- 新設キー: なし。

## 6. テスト戦略

- **reflection job**: TDD で新規作成。
  - 検知差分 (closed − (`done` ∪ `dead` ∪ `next_retry_at` 未到来の `retry`))
  - 実行枠の分類・順序 (未試行新しい順 2 + eligible 古い順 8、融通)
  - plan 文脈あり / なし (plan_id NULL) の両経路
  - LLM 失敗 → `retry` 行保存 (attempt_count 増分 + backoff) → next_retry_at
    到来後に再処理される
  - RAG 成功 + `done` 記録失敗 → 次回再処理で冪等
  - `done` / `dead` は再処理しない。`retry` は next_retry_at 到来後のみ再処理
- **retry 管理** (§3.2b): 失敗→retry→backoff→dead の遷移、instrument 不在→即 dead、
  新規優先 2 + backfill 8 の枠配分、dead の検知除外。
- **実行制御** (§3.6): slot busy で controller 即終了、planning 実行中で譲る、
  `waiting_user_job` で譲る、stale planning run は実行中と見なさない、
  起動時 dangling 回収、guard が controller 完了まで保持される。
- **reflections テーブル**: OrchestratorStore migration テスト既存パターンに追加。
- **directional RAG cleanup** (§3.4b / 再レビュー Medium-5):
  - 複合 filter が `session_type=trade AND phase=complete` のみ返す
  - cleanup が forecast / HOLD / entry カードだけを削除する
  - cleanup の再実行が冪等
  - trade complete カードは維持される
- **fail-fast 起動** (§1.5): enabled=false / bootstrap 失敗 → 起動中止、
  mode=shadow → warning のテストを追加。
- **削除系**: 削除ごとに (a) 参照残りゼロ (grep + import 確認)、
  (b) 影響ファイルの per-file pytest green。
- **既存テストの整理**: 削除対象 (trading cycle / forecast / accuracy / rag_adjustment /
  session / hold / adaptive / audit) のテストは削除。共有部 (TradeSignal /
  _calculate_position_size / exit_check / _helpers) のテストは残し green を確認。
- **回帰基準** (レビュー Medium-7 対応): per-file green は作業中の
  イテレーション用にとどめ、**合格基準は full suite `uv run pytest` で
  既知失敗のみ** (CLAUDE.md 基準: `tests/test_insights.py` ChromaDB 系 2 件。
  失敗が既知集合から増えていないことを確認)。本変更は import / CLI / API /
  scheduler / config / DB を横断するため、collection-time の import error は
  full run でしか検出できない。**discord_bot 側も全テスト実行**を合格基準に含める。

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
- **デプロイ順序** (§2.2b): bot 側の削除は旧 finance API とも互換
  (導線を消すだけ) なので、404 窓を作らない順序は:
  1. discord_bot 更新 (forecast / run_trade 導線削除)
  2. finance 停止 → バックアップ (§4) → migration 実行
  3. finance 更新・起動 (fail-fast 検証が通ることを確認)
  4. 疎通確認 (orchestrator 稼働・reflection job・bot コマンド)
- technical-llm-omit (未マージ、0 ベース migration 必須) とマージ順序・デプロイ順序を
  合わせて計画すること。
- 停止対象 job が消えることで Schedule 表示・health エンドポイントの job 一覧も変わる。
  discord_bot 側の `/health` 表示に取引サイクル/forecast 依存があれば追従が必要
  (実装時に確認)。
