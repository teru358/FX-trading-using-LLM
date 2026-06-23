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

mode 分岐:
- `mode=shadow` (既定): 従来通り。broker 未結線、shadow_trigger 記録のみ。**完全な後方互換・回帰維持。**
- `mode=live`: 上記 F 経路が有効。bootstrap が live 時のみ execution broker + execution position_mgr を runtime に注入。

**Phase 2/D との独立性:** `tick_migration_stage` (保護移設の段階) と `mode` (発注の段階) は独立した2軸。`protect_live` (保護を worker が適用) と `mode=live` (entry を orchestrator が発注) は別概念。F は `mode` のみ扱う。

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
   broker 送信の**直前**に `mark_order_submitted(plan_id, submitted_at=now)`。これ以降のクラッシュは `needs_reconcile`。

5. **発注 (single writer)**
   `result = broker.execute_signal(signal, position_mgr, macro_context)`。signal は plan の `action_json` (direction/sl/tp/rr) + ロット設定から `TradeSignal` を構築。

6. **結果反映**
   - `executed` → order_intent を `status=submitted, order_id, broker_result` 更新。`ExecutionResult` を `orchestrator_decisions` (order_id / risk_gate_result) に反映。
   - `skipped` → 想定内抑制。order_intent に記録 (alert なし)。
   - `rejected` / `halted` / `failed` → order_intent に結果記録 + decision 反映 + **alert** (要注意分類)。

### エラー隔離

`_execute_live_trigger` 全体の例外は watch loop を止めない (既存 `_evaluate_plan` の try/except 思想)。例外時は order_intent の状態 (submitted_at 有無) に応じて起動時 recovery job が後で拾う。

### signal 構築 / ロット

plan の `action_json` から `TradeSignal` を組む。ロットは既存 `trading` config のロット設定 (限定発注の小ロットはここで表現)。signal 構築の詳細フィールド対応は plan で確定。

---

## 3. F-2: durable order lock + クラッシュ復旧

### 既存実装 (再利用)

- `_OrderIntent` ORM (`order_intents` テーブル、`plan_id` UNIQUE、カラム: `owner_run_id` / `lease_until` / `submitted_at` / `recovery_status` / `status` / `order_id` / `broker_result`)
- store API: `try_insert_order_intent` / `get_order_intent` / `mark_order_submitted` / `find_expired_pending`

### F で追加

1. **store API 補完** (既存に無い分のみ — 実装時に既存メソッドを確認し不足分のみ追加)
   - `mark_order_result(plan_id, status, order_id, broker_result)` — 発注応答反映
   - `mark_order_rejected(plan_id, reason)` — ゲート/broker reject 記録
   - `set_recovery_status(plan_id, recovery_status)` — recovery job 用

2. **起動時 recovery job (§8.8 の3分岐)**
   起動時に `find_expired_pending(now)` を列挙し、`submitted_at` の有無で判定:

   | 状態 | recovery_status | アクション |
   |---|---|---|
   | `submitted_at` null | `retryable` | 未発注。reconciliation 後に plan 再 trigger 可 (plan を active に戻す等) |
   | `submitted_at` あり・`order_id` なし | `needs_reconcile` | broker に建玉したか不明。**当該 plan の再 trigger を禁止** + **alert**。自動照合はしない (手動 / 既存 reconciliation) |
   | `submitted_at` あり・`order_id` あり | (正常) | status を `submitted` に補正のみ |

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

ゲート reject 時: 発注しない + order_intent rejected + decision 反映 + alert。plan を再評価対象に戻すか終了させるか (一時的 reject=再評価可 / 恒久的=終了) は plan で詳細化。

---

## 5. F-4: mode 昇格 + bootstrap 結線

### config

- `OrchestratorConfig.mode` (既存フィールド `mode: str = "shadow"`、コメント "observe | shadow | live") を扱う。**F が執行を有効化するのは `mode=live` のときのみ。** `shadow` (既定) は従来の shadow 経路。`observe` は現状未使用であり F の執行段は起動しない (= shadow と同じく非 live 扱い、broker 未結線)。`__post_init__` の許容値 validation は実装時に既存実態を確認して追加 (shadow/live を最低限サポートし、observe は非 live として安全側)。
- 限定発注は既存 config で表現:
  - 限定銘柄 → `orchestrator.pairs` (planning scope)
  - 小ロット → `trading` のロット設定
- 新しい mode 軸は増やさない。
- material recheck: `OrchestratorConfig` に `execution_opinion_recheck_enabled: bool = False` + `execution_recheck_timeout_seconds`(既存があれば再利用) を追加。

### bootstrap 結線

- `mode=live` のときのみ、execution 用 `BrokerAdapter` (発注可能な broker = `create_broker(...)` / 既存の発注用ファクトリ。`build_close_broker` は close 専用ラッパなので発注には execution 用を使う — 実装時に trading cycle の broker 構築 `src/cycles/trading.py` の `create_broker` 呼び出しに倣う) と execution 用 `PositionManager` を構築し runtime に注入。
- `mode=shadow` では broker 未注入 → runtime は broker None なら live 分岐に入らないガード → `_execute_live_trigger` は呼ばれない (回帰維持)。
- 旧 trading cycle の新規発注: live 時は main.py で停止 (mode=live なら旧 cycle の entry 登録 skip or 既存 disable フラグ)。**コード削除は別 cleanup task。**

---

## 6. テスト方針 (TDD)

- **F-1:** trigger→gate→execute フロー (gate pass で発注 / reject で不発注)。executed/skipped/rejected/failed の各 ExecutionResult が order_intent + decision に正しく反映されるか。
- **F-2:** order_intents UNIQUE で二重発注防止。recovery 3分岐それぞれ (retryable / needs_reconcile が再 trigger 禁止 + alert / 正常 status 補正)。`submitted_at` 前後でのクラッシュ模擬。
- **F-3:** broker reject が decision に反映されるか。pre-check pass + final reject の不一致が記録されるか。
- **F-4:** `mode=shadow` で broker 未結線 (回帰)。`mode=live` で broker 注入され執行段が動く。mode validation。

実 broker / LLM はテストで mock (コスト抑制・本番発注を起こさない)。

---

## 7. Review Checklist

- [ ] F-1: 発注前に RiskGateWorker final + broker gate を必ず通るか。
- [ ] F-1: single execution writer — `broker.execute_signal` を呼ぶのは `_execute_live_trigger` 1箇所のみか。
- [ ] F-1: material recheck が既定 OFF で、決定的・高速執行 (LLM なし) が既定経路か。
- [ ] F-2: `order_intents.plan_id` UNIQUE で二重発注を防ぎ、クラッシュ復旧3分岐が動くか。
- [ ] F-2: needs_reconcile が再 trigger 禁止 + alert で、自動照合はしない (スコープ境界) か。
- [ ] F-3: broker reject / 両 gate 不一致が decision に反映されるか。
- [ ] F-4: `mode=shadow` (既定) で broker 未結線・shadow 全テスト回帰グリーンか。
- [ ] F-4: 旧 trading cycle 発注が live 時に停止し、コード削除は行っていない (omit は別 task) か。

---

## 8. スコープ外 (将来 / 別 task)

- 旧 trading cycle 発注経路の **コード削除 (omit)** — live 安定確認後の version2 完全移行 cleanup。
- `needs_reconcile` の **broker 自動照合** — 既存 reconciliation 経路の能力確認後に別 task。
- material recheck の **既定 ON 化** — live 初期検証後に必要なら。
- OANDA `LiveBrokerAdapter` の発注実装 (現状 mt5 が主経路)。

関連: `2026-06-21-orchestrator-phase1to3-cadence-tick-execution-design.md` §F (親) / `2026-06-20-orchestrator-agent-loop-design-v2.md` (v2 全体)
