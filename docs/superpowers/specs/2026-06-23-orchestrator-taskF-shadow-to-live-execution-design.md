# Orchestrator Task F — shadow→本番発注 昇格 (執行段結線) 実装設計 Spec

**Date:** 2026-06-23
**Status:** Draft for review
**Parent spec:** `2026-06-21-orchestrator-phase1to3-cadence-tick-execution-design.md` §F (本 spec はその §F の実装設計)
**Branch:** `feat/planner-watch-loop` (継続)
**Scope:** orchestrator の watch trigger を本番発注へ接続する最終フェーズ (Task F)。broker 結線 + durable order lock + クラッシュ復旧 + mode 昇格。

---

## 0. 背景と前提

これは finance トレードシステムの **version2 再設計** (orchestrator agent loop) の最終フェーズ。既存コードを再利用しつつ orchestrator を組み直しており、現行システムに対して version2 の位置づけ。

**現在の到達点 (Task A〜D 完了時点):** version2 orchestrator は **shadow (paper) トレードまで完了**。plan→watch trigger→shadow_trigger 記録、ポジション保護移設 (Phase 2/D)、hindsight メトリクス収集が動く。**broker / order_intents / 本番発注には触れない (shadow 境界)。**

**Task F の役割:** **shadow → live の切り替えを可能にする**。orchestrator 執行段を本番発注に結線し、`OrchestratorConfig.mode` を `shadow`→`live` に昇格できるようにする。

**F の後:** live 動作確認後、**不要コード (旧 trading cycle の新規発注経路など) のオミット** を行う (version2 完全移行の cleanup、本 spec のスコープ外・別 task)。

**着手前提 (親 spec §F より):** F は技術タスクというより、shadow メトリクス (hindsight MFE-R/PnL-R 等) が十分溜まり Phase 1〜2 が安定してからの踏み切り判断。最後。本 spec は plan 作成までを行い、実装着手タイミングは別途判断する。

### 確定した設計判断 (2026-06-23 brainstorming)

1. **発注主体:** live 時は **orchestrator 執行段が単一の発注主体 (single execution writer)**。既存 trading cycle の新規 entry 発注は live 時に停止 (shadow 降格 / disable)。position 管理 / reconciliation / close は既存系を再利用。
2. **mode 粒度:** `OrchestratorConfig.mode` は `shadow` / `live` の2値 (既存フィールド、既定 shadow)。限定発注 (限定銘柄・小ロット) は既存 config (`orchestrator.pairs` / `trading` のロット設定) で表現。mode 軸を増やさない。
3. **クラッシュ復旧 reconcile 範囲:** `needs_reconcile` は **検出・隔離 (再 trigger 禁止)・alert まで**。broker への自動問い合わせ照合は F に含めない (既存 reconciliation 経路 or 手動)。
4. **material recheck:** F-1 の material change 時 ExecutionOpinionAgent 再点火は **配線するが config フラグで既定 OFF**。まず決定的・高速執行 (LLM なし) で live 検証。
5. **執行コンテキスト:** trigger 確定直後に同一 watch スレッド内で同期執行 (案A、claim-first / submit-marked 一気通貫)。
6. **旧経路 omit:** F では旧 trading cycle 発注を **フラグで停止するのみ**。コード削除は live 安定確認後の別 cleanup task。

### 不変条件

- **single execution writer:** `broker.execute_signal` を呼ぶのは執行段 (`_execute_live_trigger`) の1箇所のみ。
- **shadow 境界維持:** `mode=shadow` (既定) では F 経路に一切入らない。既存 shadow 全テストが回帰グリーン。後方互換。
- **二重発注の構造的防止:** `order_intents.plan_id` UNIQUE が唯一の発注を保証 (多層防御)。
- **後方互換:** 全て `mode` フラグ (既定 shadow) でガード。

---

## 1. アーキテクチャ概要

live mode 時の経路:

```
watch loop (run_watch_cycle)
  └─ _evaluate_plan → entry 成立
       └─ _record_shadow_trigger              ← 既存 (shadow_trigger 記録は live でも維持)
            └─ [F: mode=live 分岐] _execute_live_trigger   ← 新規・single writer
                 1. try_insert_order_intent(plan_id, pending)   ← UNIQUE で二重発注防止
                 2. (material recheck: 既定 OFF)
                 3. RiskGate final gate + broker authoritative gate (F-3)
                 4. mark_order_submitted(submitted_at)          ← broker 送信直前 (復旧分岐点)
                 5. broker.execute_signal(signal, position_mgr)
                 6. order_intent 更新 (status/order_id/broker_result) + ExecutionResult を decision 反映
```

mode 分岐 (`OrchestratorConfig.mode`):
- `shadow` (既定): 従来通り。broker 未結線、shadow_trigger 記録のみ。**完全な後方互換・回帰維持。**
- `live`: 上記 F 経路が有効。bootstrap が live 時のみ execution broker + execution position_mgr を runtime に注入。**ただし本番発注には top-level `AppConfig.mode=live` + `live_broker` が前提** (§5 の cross-field validation 参照)。

**Phase 2/D との独立性:** `tick_migration_stage` (保護移設の段階) と `OrchestratorConfig.mode` (発注の段階) は独立した2軸。`protect_live` (保護を worker が適用) と `mode=live` (entry を orchestrator が発注) は別概念。F は `OrchestratorConfig.mode` のみ扱う。

---

## 2. F-1: 執行段の broker 結線 (案A)

`mode=live` のとき、`_record_shadow_trigger` が shadow_trigger 記録に成功した**直後**、同一 watch スレッド内で `_execute_live_trigger(plan, pair, quote, decision_id, shadow_trigger_id, run_id)` を同期実行する。

### ステップ

1. **order_intent claim**
   `try_insert_order_intent(plan_id, pair, status="pending", owner_run_id=run_id, lease_until=now+TTL)`。
   UNIQUE 違反 (既に intent 存在) なら **既発注として中止** (log + return)。

2. **material recheck (既定 OFF)**
   `execution_opinion_recheck_enabled=false` (既定) ならスキップ。true かつ material change フラグ時のみ ExecutionOpinionAgent を軽く再点火。`execution_recheck_timeout_seconds` 超過時は当該 plan 発火を保留 (安全側、order_intent を pending のまま残し次 tick 判断 or abandon — plan で詳細化)。

3. **二段ゲート (F-3)** — §4 参照。reject なら発注せず order_intent を rejected + decision 反映 + alert。

4. **submit マーキング (復旧分岐点)**
   broker 送信の**直前**に `mark_order_submitted(plan_id, submitted_at=now)`。**注意 (codex High #1):** 既存 `mark_order_submitted` は `submitted_at` を埋めると同時に `status="submitted"` にする (`orchestrator_store.py:613-614`)。よって「送信直後クラッシュ」は `status="submitted"` かつ `order_id is null` の状態になる。recovery query (§3) はこれを拾う必要がある (現状 `get_stale_pending_intents` は `status=="pending"` のみ — §3 で拡張する)。

5. **発注 (single writer)**
   `result = broker.execute_signal(signal, position_mgr, macro_context)`。signal は plan の `action_json` (direction/sl/tp/rr) + ロット設定から `TradeSignal` を構築。

6. **結果反映 (ExecutionResult.outcome → order_intent.status の明示 mapping、codex High #2)**
   既存 status enum は `pending/submitted/filled/rejected/failed/needs_reconcile/abandoned` (`ORDER_INTENT_STATUSES`, `orchestrator_store.py:105`)。`record_order_result` は enum 外を ValueError にする。`ExecutionResult.outcome` (executed/skipped/halted/rejected/failed) を以下へ mapping して `record_order_result` に渡す:

   | ExecutionResult.outcome | order_intent.status | order_id | alert | 備考 |
   |---|---|---|---|---|
   | `executed` | `filled` | あり | なし | 約定。既存テスト (`test_orchestrator_store.py:120`) と整合 |
   | `skipped` | `abandoned` | null | なし | 想定内抑制 (既存ポジ/hold/リスク制限)。plan は終了扱い |
   | `rejected` | `rejected` | null | **あり** | gate/broker 拒否 |
   | `halted` | `rejected` | null | **あり** | halt 状態。reject 同様に発注見送り |
   | `failed` | `failed` | null | **あり** | 技術的失敗 (bridge 不通等) |

   `ExecutionResult` (outcome / order_id / reason) は併せて `orchestrator_decisions` (order_id / reject 理由 / risk_gate_result) に反映する。
   > `skipped→abandoned` / `halted→rejected` の mapping は実装時に既存テストと突き合わせて最終確定 (この表が plan の起点)。

### エラー隔離

`_execute_live_trigger` 全体の例外は watch loop を止めない (既存 `_evaluate_plan` の try/except 思想)。例外時は order_intent の状態 (submitted_at 有無) に応じて起動時 recovery job が後で拾う。

### signal 構築 / ロット

plan の `action_json` から `TradeSignal` を組む。ロットは既存 `trading` config のロット設定 (限定発注の小ロットはここで表現)。signal 構築の詳細フィールド対応は plan で確定。

---

## 3. F-2: durable order lock + クラッシュ復旧

### 既存実装 (再利用) — 実 API 名で記載

- `_OrderIntent` ORM (`order_intents` テーブル、`plan_id` UNIQUE、カラム: `plan_id` / `trigger_id` / `decision_id` / `pair` / `intended_action` / `status` / `owner_run_id` / `lease_until` / `submitted_at` / `recovery_status` / `order_id` / `broker_result_json` / `created_at` / `updated_at`、`orchestrator_store.py:111`)
- status enum: `ORDER_INTENT_STATUSES = pending/submitted/filled/rejected/failed/needs_reconcile/abandoned` (`orchestrator_store.py:105`)
- store API: `try_insert_order_intent` (`:527`) / `get_order_intent` (`:579`) / `get_stale_pending_intents` (`:587`) / `mark_order_submitted` (`:605`) / `record_order_result` (`:618`)

### F で追加

1. **store API 補完**
   - **recovery query の拡張 (codex High #1):** 既存 `get_stale_pending_intents` は `status=="pending"` のみ拾う (`orchestrator_store.py:597`)。だが `mark_order_submitted` は `status="submitted"` にしてしまうため (`:614`)、最重要の「送信直後クラッシュ」(`status=submitted` かつ `order_id is null`) が拾われない。**recovery 候補取得を `status=="pending"` OR (`status=="submitted"` AND `order_id is null`) の両方を lease 超過で拾う形に拡張する** (既存メソッド拡張 or 新メソッド追加 — 実装時に既存呼出側との互換を確認)。
   - `record_order_result(plan_id, status, order_id, broker_result_json)` は**既存** (`:618`)。reject/failed もこれで記録 (status を enum 値で渡す)。専用 `mark_order_rejected` は不要 (record_order_result に reject を渡す)。
   - `set_recovery_status(plan_id, recovery_status)` — recovery job 用に**新規追加** (`recovery_status` カラムは既存だが更新 API が無い)。

2. **起動時 recovery job (§8.8 の3分岐)**
   起動時に拡張した recovery query で lease 超過の未完了 intent を列挙し、`status` + `submitted_at` + `order_id` で判定:

   | 状態 | recovery_status | アクション |
   |---|---|---|
   | `status=pending` (submitted_at null) | `retryable` | 未発注。reconciliation 後に plan 再 trigger 可 (§4 の reject 同様 plan を active に戻す) |
   | `status=submitted` かつ `order_id` null | `needs_reconcile` | broker に建玉したか不明。**当該 plan の再 trigger を禁止** + **alert**。自動照合はしない (手動 / 既存 reconciliation) |
   | `status=submitted` かつ `order_id` あり | (正常) | status を `filled` に補正のみ (約定済とみなす) |

3. **再 trigger 禁止の担保**
   `needs_reconcile` の plan は watch loop が trigger 対象から除外 (order_intents 参照 or plan 状態)。`try_insert_order_intent` の UNIQUE が残るため、仮に再評価されても二重 insert は弾かれる (多層防御)。

**スコープ境界:** `needs_reconcile` は検出・隔離・alert まで。broker 自動照合は F 外。

---

## 4. F-3: broker final gate と二段構え

1. **RiskGate pre-check (既存・decision 前)**
   現状 `_shadow_risk_precheck` が trigger 前に走り結果を `shadow_triggers` / `orchestrator_decisions.risk_gate_result` に残す (hard veto せず記録のみ、LLM コスト節約)。shadow/live 共通で維持。

2. **broker final authoritative gate (F 新規・発注直前)**
   `_execute_live_trigger` ステップ3で発注直前に最終判定:
   - RiskGate を**発注確定モード**で再評価 (pre-check は記録のみだったが、ここは hard gate = reject なら発注しない)
   - broker authoritative gate (halt 状態・証拠金・broker 側制約)

3. **decision 反映**
   broker `ExecutionResult` (outcome / order_id / reason) を必ず `orchestrator_decisions` に反映 (order_id / reject 理由 / risk_gate_result)。両 gate (pre-check と final) の結果を残し**不一致を検出可能**にする。

### reject/failed/halted 後の plan / order_intent 状態遷移 (codex High #4 — 必須確定)

**問題:** `_record_shadow_trigger` は trigger 時点で plan を `active`→`triggered` に claim する (`runtime.py:569` `try_mark_plan_triggered`)。`get_active_plans` は `active` のみ再評価する (`orchestrator_store.py:514`)。さらに order_intent は `plan_id` UNIQUE。よって final gate reject 後に何もしないと、plan は `triggered` のまま再評価されず、order_intent が UNIQUE を握り**永久ブロック**になる。これを spec で固定する:

| ケース | order_intent.status | plan 状態 | 再発注可否 |
|---|---|---|---|
| **一時的** reject / `halted` / `failed` (halt 一時 / 一時的拒否 / bridge 不通) | `rejected` or `failed` | plan を **`active` に戻す** (`update_plan_status(active)`) **かつ order_intent を削除 or abandoned 化** して UNIQUE を解放 | 次 tick で再 trigger 可 (entry 条件がまだ成立していれば) |
| **恒久的** reject (invalid stops / 証拠金恒久不足 / risk hard veto) | `rejected` | plan を **`invalidated`** にする | 再 trigger しない (plan 終了) |
| `skipped` (想定内抑制) | `abandoned` | plan を `invalidated` (想定内に発注見送り = この plan は用済み) | 再 trigger しない |

**UNIQUE 解放の方針 (重要):** 一時的失敗で再発注を許すには、`triggered`→`active` に戻すだけでなく **order_intent の UNIQUE 制約を解放する必要がある** (同 plan_id で再 insert できないと §2 step1 で必ず中止になる)。F では「一時的失敗時は order_intent を `abandoned` にし、recovery/再 trigger 経路は **新しい plan** (replan) で発注する」方針を既定とする — 同一 plan_id の再 insert を避け UNIQUE 設計を壊さない。`triggered`→`active` 復帰 + 同 plan_id 再 insert を許す案は UNIQUE と衝突するため採らない。
> 一時的 reject の「再評価」を同一 plan で行うか replan で行うかは、上記「replan 既定」で固定。plan 作成時にこの遷移をテストで pin する。

reject/failed/halted いずれも: 発注しない + order_intent に上記 status + decision 反映 + alert (skipped 除く)。

---

## 5. F-4: mode 昇格 + bootstrap 結線

### config — 2つの mode の関係 (codex 追加確認 — 必須)

**重要:** `OrchestratorConfig.mode` (`shadow`/`live`、`schema.py:678`) と トップレベル `AppConfig.mode` (`paper`/`live`/`live_test`、`schema.py:727`) は**別物**。発注 broker は **トップレベル `AppConfig.mode` + `live_broker`** で選ばれる (`create_broker(mode, live_broker, ...)`, `live_broker.py:140`)。`OrchestratorConfig.mode=live` だけでは本番 broker にならない。

- **cross-field validation を追加 (必須):** `OrchestratorConfig.mode=="live"` のとき、`AppConfig.__post_init__` (または loader) で **`AppConfig.mode=="live"` かつ `live_broker` が設定済 (mt5 等)** を要求し、満たさなければ `ValueError`。orchestrator が本番発注すると言いながら top-level が paper だと、執行段が paper broker を握る (本番発注にならない) か矛盾するため。`OrchestratorConfig` 単体の `__post_init__` では AppConfig を参照できないので、この検証は AppConfig レベルに置く。
- `OrchestratorConfig.mode` の許容値: F では `shadow`/`live` を扱う。`observe` は現状未使用で F の執行段を起動しない (= 非 live 扱い、broker 未結線)。許容値 validation は実装時に既存実態を確認して追加。
- 限定発注は既存 config で表現: 限定銘柄 → `orchestrator.pairs` (planning scope) / 小ロット → `trading` のロット設定。新しい mode 軸は増やさない。
- material recheck: `OrchestratorConfig` に `execution_opinion_recheck_enabled: bool = False` + `execution_recheck_timeout_seconds` (既存があれば再利用) を追加。

### bootstrap 結線

- `OrchestratorConfig.mode=live` のときのみ、execution 用 `BrokerAdapter` を構築し runtime に注入。**broker は trading cycle と同じファクトリ `create_broker(mode=AppConfig.mode, live_broker=..., ...)` を使う** (`src/cycles/trading.py:1026` に倣う。`build_close_broker` は close 専用ラッパなので発注には execution 用を使う)。execution 用 `PositionManager` も構築。
- `OrchestratorConfig.mode=shadow` では broker 未注入 → runtime は broker None なら live 分岐に入らないガード → `_execute_live_trigger` は呼ばれない (回帰維持)。

### single execution writer の担保 (codex High #3 — main.py 停止だけでは不十分)

旧 `run_trading_cycle` は **4 つの entry point** から起動できる: `main.py:414` (scheduler) / `api/routes/trading.py:70` (API) / `cli.py:369` (CLI) / `tui.py:322` (TUI)。旧 cycle は内部で `create_broker` し (`trading.py:1026`) `execute_signal` を呼ぶ (`trading.py:754`)。**main.py のスケジュール登録を止めるだけでは API/CLI/TUI 経由の発注が残り、single execution writer が崩れる (二重発注リスク)。**

**F の方針 (統一ガード):** `run_trading_cycle` の**内部で entry phase をガードする**。`OrchestratorConfig.mode=="live"` のとき、`run_trading_cycle` は新規 entry (`execute_signal` 呼出) を skip する (exit/position 管理/reconciliation は継続)。これにより呼出元 (main/API/CLI/TUI) に関わらず**新規発注は orchestrator 執行段 1 経路に集約**される。
- ガードは `run_trading_cycle` の entry phase 直前に 1 箇所追加 (全 entry point を一括カバー)。
- 旧 cycle の exit/close/reconciliation は触らない (既存系の再利用は維持)。
- **コード削除 (旧 entry phase の omit) は別 cleanup task** (live 安定後の version2 完全移行)。F では「フラグで entry を停止」まで。

---

## 6. テスト方針 (TDD)

- **F-1:** trigger→gate→execute フロー (gate pass で発注 / reject で不発注)。`ExecutionResult.outcome → order_intent.status` mapping (executed→filled / skipped→abandoned / rejected→rejected / halted→rejected / failed→failed) が正しく `record_order_result` に渡るか。
- **F-2:** order_intents UNIQUE で二重発注防止。**recovery query が `status=submitted` かつ `order_id is null` (送信直後クラッシュ) を拾うか** (codex #1 回帰)。recovery 3分岐それぞれ (retryable / needs_reconcile が再 trigger 禁止 + alert / 正常 filled 補正)。`submitted_at` 前後 (status=pending vs submitted) のクラッシュ模擬。
- **F-3:** broker reject が decision に反映されるか。pre-check pass + final reject の不一致が記録されるか。**reject/failed/halted 後の plan/order_intent 遷移** (一時的→abandoned+replan / 恒久的→invalidated) で永久ブロックが起きないか (codex #4 回帰)。
- **F-4:** `OrchestratorConfig.mode=shadow` で broker 未結線 (回帰)。`mode=live` で broker 注入され執行段が動く。**`mode=live` かつ `AppConfig.mode!=live` で ValueError** (codex 追加確認 回帰)。**`OrchestratorConfig.mode=live` のとき `run_trading_cycle` が entry phase を skip するか** (全 entry point カバー、codex #3 回帰)。

実 broker / LLM はテストで mock (コスト抑制・本番発注を起こさない)。

---

## 7. Review Checklist

- [ ] F-1: 発注前に RiskGateWorker final + broker gate を必ず通るか。
- [ ] F-1: single execution writer — `broker.execute_signal` を呼ぶのは `_execute_live_trigger` 1箇所のみか。
- [ ] F-1: material recheck が既定 OFF で、決定的・高速執行 (LLM なし) が既定経路か。
- [ ] F-1: `ExecutionResult.outcome → order_intent.status` mapping が enum (`ORDER_INTENT_STATUSES`) 内に収まるか (executed→filled 等、codex #2)。
- [ ] F-2: `order_intents.plan_id` UNIQUE で二重発注を防ぎ、クラッシュ復旧3分岐が動くか。
- [ ] F-2: recovery query が `status=pending` だけでなく `status=submitted & order_id is null` (送信直後クラッシュ) を拾うか (codex #1)。
- [ ] F-2: needs_reconcile が再 trigger 禁止 + alert で、自動照合はしない (スコープ境界) か。
- [ ] F-3: broker reject / 両 gate 不一致が decision に反映されるか。
- [ ] F-3: reject/failed/halted 後の plan/order_intent 遷移が定義され、永久ブロック・再評価不能が起きないか (codex #4)。
- [ ] F-4: `mode=shadow` (既定) で broker 未結線・shadow 全テスト回帰グリーンか。
- [ ] F-4: `OrchestratorConfig.mode=live` が `AppConfig.mode=live` + `live_broker` を要求する validation があるか (codex 追加確認)。
- [ ] F-4: `OrchestratorConfig.mode=live` 時、`run_trading_cycle` の entry phase が全 entry point (main/API/CLI/TUI) で停止し、コード削除は行っていない (omit は別 task) か (codex #3)。

---

## 8. スコープ外 (将来 / 別 task)

- 旧 trading cycle 発注経路の **コード削除 (omit)** — live 安定確認後の version2 完全移行 cleanup。
- `needs_reconcile` の **broker 自動照合** — 既存 reconciliation 経路の能力確認後に別 task。
- material recheck の **既定 ON 化** — live 初期検証後に必要なら。
- OANDA `LiveBrokerAdapter` の発注実装 (現状 mt5 が主経路)。

関連: `2026-06-21-orchestrator-phase1to3-cadence-tick-execution-design.md` §F (親) / `2026-06-20-orchestrator-agent-loop-design-v2.md` (v2 全体)
