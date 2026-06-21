# Orchestrator 残タスク (A〜D + F) — 実装設計 Spec

**Date:** 2026-06-21
**Status:** Draft for review
**Parent spec:** `2026-06-20-orchestrator-agent-loop-design-v2.md` (本 spec はその残タスクの実装設計)
**Branch:** `feat/planner-watch-loop` (継続)
**Scope:** orchestrator Phase 2〜6 完了後の未実装項目。collection cadence の動的制御 + shadow 完成 (A〜C)、websocket tick 基盤 + ポジション保護移設 (D)、shadow→本番発注昇格 (F)。

---

## 0. 背景と前提

orchestrator の Phase 2〜6 (shadow decision / watch loop / hindsight / 通知 / 本番 lifecycle 結線) は **全完了** (HEAD `11d0fd9`, full suite 1089 passed)。spec v2 全体ではまだ未実装項目があり、実装負荷と本番影響度で 3 フェーズ + F に分割する。

**用語注意 (spec v2 由来):** 「watch」は 2 系統ある。
- **(1) watch loop** (§5.1 `WatchEvaluator` — active plan を tick 評価する非LLM 自走スレッド群)
- **(2) watch instrument** (§4.8 `watch_only_instruments` — 監視専用銘柄種別)

D の「保護移設の watch 側」は **(1) watch loop** の意味。B/C の「boost は trade のみ」は **(2) watch instrument** を除外する意味。

**全タスク共通の不変条件 (A〜D):**
- shadow 境界を維持する (broker / order_intents / 本番ポジション保護に触れない)。**F のみ shadow 境界を越える** (本番発注を有効化する)。
- 後方互換: 新機能は config フラグ (既定 false / disabled) でガードし、既存 trading cycle・既存 scheduler に影響させない。段階導入を保つ。
- TDD (RED→GREEN)。各タスク後に code review。実 LLM/RAG はテストで mock (コスト抑制)。

**実装順:** A → B → C → D → F。A〜C は依存順 (C は B の resolver に接続)。D は A〜C と独立だが tick 基盤の上に保護移設が乗るため D-1→D-2。F は shadow メトリクスが十分溜まった後の運用判断を伴う最後の踏み切り。

---

## Phase 1 — A〜C: 収集頻度の動的制御 + shadow 完成

shadow 境界内。全て非LLM / 配線 / 状態管理。

### Task A — 配線のみ (軽・即効)

`MaterialLandingDetector` / `ShadowNotifier.notify_daily_summary` / `compute_shadow_metrics` は実装済。**配線が無いだけ**なので新規ロジックは最小。

#### A-1: daily summary 発火配線 (§11)

- **config 追加:** `OrchestratorNotificationsConfig.daily_summary_time: str = "07:00"` (JST、`schedule.timezone` に従う)。
- **runtime 追加:** `OrchestratorRuntime.run_daily_summary_cycle(now)` — `compute_shadow_metrics(orch_store, now)` → `notifier.notify_daily_summary(metrics)`。`notifications.shadow_daily_summary` が false / notifier 未注入なら no-op。
- **発火方式:** schedule ライブラリに依存せず runtime 内で完結する「1日1回ガード」。`_last_summary_date: date | None` を持ち、`now` のローカル日付が前回送信日と異なり、かつ `now.time() >= daily_summary_time` なら 1 回送って日付を記録する。テスト容易 (now 注入で決定論)。
- **ループ:** `_daily_summary_loop` を追加し `start()` / `stop()` に組み込む。notifier 未注入 (or `shadow_daily_summary=false`) なら起動しない。ポーリング周期は `market_state.normal_seconds` 程度の粗さで十分 (日次判定なので)。

#### A-2: news/event landing 配線 (§5.4①)

`MaterialLandingDetector` は `get_news_impact` / `get_news_key` / `in_event_window` / `get_event_key` を既にサポート。`bootstrap._build_detector` で未注入のため常時 False に倒れている。これを実 provider で配線する。

- **`make_news_material_provider(store)`** (bootstrap, 新規): pair 関連の直近 news impact (絶対値) と key を返す 2 関数。既存 `aggregate_news_sentiment` / news 集計を流用 (`make_news_provider` と同じ source)。key は「news の identity」(最新 news の id / timestamp 由来) で、同一 news の二度発火を防ぐ。
- **`make_event_window_provider(config, econ_store)`** (bootstrap, 新規): `EconEventStore` から high-importance イベントを引き、`[event_time − 10min, event_time + 30min]` 窓内かを返す `in_event_window(pair)` と、その window identity を返す `get_event_key(pair)`。pair→通貨マッピングで該当イベントのみ。`EconEventStore.get_recent_published` 等の既存 API を流用 (必要なら upcoming 窓取得の薄い helper を追加)。
- **`_build_detector` 変更:** 上記を注入。**pairs は既に tradeable のみ** (§4.8) なので watch 除外は追加不要。news/event callable 未取得時 (store 無し等) は None のまま (既存の安全な False 挙動)。

**テスト:** A-1 は run_daily_summary_cycle の 1 日 1 回ガード (時刻跨ぎ / 同日二度目 skip / フラグ off)。A-2 は news/event provider の window/key 判定と detector 配線後の `is_material` 経路 (technical 無しでも news 単独 / event 単独で material になること、同一 key 二度目 skip)。

---

### Task B — cadence resolver + 可変 interval ドライバ (中・本体)

§5.3 (3 経路 boost most-aggressive-wins・TTL) + §5.6 (self-scheduling 可変 interval ドライバ)。**ユーザー要望の本体。**

#### B-1: `CadenceResolver` (新規 `src/orchestrator/cadence_resolver.py`、非LLM 純ロジック)

- **boost store:** `(pair, source) → (boosted_interval_sec, expires_at)` の in-memory dict。`source ∈ {"econ", "state", "planner"}`。同一 (pair, source) は上書き。
- **`set_boost(pair, source, interval_sec, expires_at)`:** boost を書く。**trade instrument のみ受理** (resolver は trade pair set を持ち、対象外 pair の boost 要求は無視 = watch は base 固定)。
- **`effective_interval(pair, now) -> int`:** 未 expire の boost のうち **最短 interval を採用** (most-aggressive-wins)。boost 無し / 全 expire なら base interval。base は `mode: trade` なら `technical_trade_interval_hours`、`watch` なら `technical_watch_interval_hours` を秒換算。
- **TTL 失効:** read 時に `expires_at <= now` の boost を除外 (lazy expire)。`prune(now)` で明示掃除も可。
- horizon 連動 (swing/day) は base interval が config 由来なので resolver は interval 値に対して透過。

#### B-2: 可変 interval self-scheduling ドライバ (§5.6、新規 `src/jobs/cadence_driver.py`)

- **薄い「毎 tick ドライバ」:** `schedule.every(1).minutes` 相当 1 本で回す。毎 tick **pair ごとに resolver を引き、`now − last_run[pair] ≥ effective_interval(pair, now)` なら収集を発火**。発火後 `last_run[pair] = now`。
- **収集発火:** trade pair は `run_trade` callback、watch pair は `run_watch` callback (既存 `run_trade_technical_collection` / `run_watch_technical_collection` を渡す)。
- **skip/backfill (§5.1.1):** LLM 収集は `PriorityJobSlot` 共有。slot busy で skip された場合、`last_run` を更新しないため次 tick で経過判定が再び真になり自然に backfill (次の定期周期まで待たない)。
- **driver は単一 slot 規律を壊さない:** watch→trade の逐次性が要る同時刻ケースは、driver が同 tick 内で watch を先に発火する順序を保つ (既存 `build_technical_dispatch` の watch→trade 思想を踏襲)。

#### B-3: boost 書き込み経路① (econ、proactive・主経路)

- **`EconCadenceSource`** (新規、bootstrap で resolver に接続): `EconEventStore` から high-importance イベントの `[event_time − 10min, event_time + 30min]` 窓を引き、該当 trade pair に `set_boost(pair, "econ", boosted_interval, expires_at=window_end)` を書く。proactive 予約 (先回り)。
- boosted_interval は config 化 (`ScheduleConfig.cadence_boost_interval_minutes: int = 5` 程度)。
- 経路② (market state) は **Task C で接続**。経路③ (planner hint) は **API のみ用意** (resolver.set_boost(source="planner") を呼べる形)、実発火は後続 (YAGNI、今は stub)。

#### B-4: 統合点 (main.py、後方互換切替)

- **config 追加:** `ScheduleConfig.cadence_enabled: bool = False` + `cadence_boost_interval_minutes: int = 5`。
- `cadence_enabled=false` (既定): 現行 union-time `build_technical_dispatch` 経路をそのまま維持 (一切変更なし)。
- `cadence_enabled=true`: scheduler に union-time dispatch の代わりに cadence_driver を 1 本登録し、resolver + EconCadenceSource を駆動。
- **既存 union-time dispatch は削除しない** (静的ケースの土台 + ロールバック先として温存)。

**テスト:** B-1 resolver (most-aggressive-wins / TTL 失効 / watch boost 無視 / base fallback)。B-2 driver (interval 経過で発火 / skip 時 last_run 不更新で backfill / watch→trade 順)。B-3 econ source (窓内 boost / 窓外 base / 該当通貨のみ)。B-4 切替 (enabled/disabled で経路分岐)。

---

### Task C — market state 検知 + regime 変化イベント (中)

§4.8 / §5.2。volatility / spread / move-rate から state (calm/active/critical) を決め、(a) cadence resolver の「②市場 state 経路」に boost を書く、(b) regime 変化イベントで PlannerAgent 再計画をトリガする。**B 完了後に resolver へ接続するのが自然。**

#### C-1: `MarketStateDetector` (新規 `src/orchestrator/market_state_detector.py`、非LLM)

- **入力:** pair ごとの直近 quote 列 (move-rate)、spread、(任意) volatility。`PriceProvider` / quote provider から軽量取得。
- **state 判定 (§5.2.1):** calm / normal / active / critical。遷移条件は spec §5.2.1 の初期案:
  - → critical: 既存ポジション SL/TP/breakeven 近傍 / spread 異常拡大 / bridge degraded
  - → active: 直近 `price_move_window` 内変動が `active_move_pct` 超 / 重要 news impact 着弾
  - → normal: active/critical 解消
  - → calm: normal が `calm_after_seconds` 継続 + ポジション無し or 安定
- **ヒステリシス必須 (§5.2.1):** 上げ (calm→active) は即時、下げ (active→calm) は安定継続を要求 (`calm_after_seconds` / `normal_after_seconds`)。閾値付近のバタつき防止。
- **horizon 連動:** `active_move_pct` 等を horizon 別に持つ (swing は鈍く / day は敏感に)。
- **config 追加:** `OrchestratorMarketStateConfig` に遷移閾値を追加 (`active_move_pct` / `price_move_window_seconds` / `calm_after_seconds` / `normal_after_seconds` / `spread_spike_pips`)。`{day,swing}` overlay は既存 policy overlay 方式に合わせる。

#### C-2: cadence への接続 (経路②)

- state が active/critical の trade pair について `resolver.set_boost(pair, "state", boosted_interval, expires_at)` を書く。TTL で calm 復帰後に自然失効。**boost は trade のみ** (B と一貫)。
- これは「resolver を上書きする独立経路」ではなく、3 経路の 1 つとして most-aggressive 競合に参加する (§5.2)。

#### C-3: regime 変化イベント (planning 発火経路①へ)

- state 遷移 (特に →active / →critical) を「regime 変化」として検知したら、`MaterialLandingDetector` の event 経路相当で当該 trade pair の planning 再計画をトリガする (§5.4①)。
- **接続方法:** `MaterialLandingDetector` に regime hint を渡す薄い callable (`in_event_window` とは別の `regime_changed(pair) -> bool` 経路、または既存 event 経路に合流) を bootstrap で注入。material フィルタ + debounce は既存機構を流用 (毎遷移で暴発させない)。
- **market state は執行を制御しない (§5.2):** 検知結果は cadence boost と planning トリガにのみ効く。watch loop の tick 評価・執行には触れない。

#### C-4: 駆動 (PriceMonitorWorker 相当)

- 既存 `price_monitor.py` は本番ポジション保護で稼働中。C では **新規 worker を作らず**、orchestrator runtime 内に軽量な state 検知ループ (`_market_state_loop`、quote 直読・非LLM) を持たせる。`market_state.*_seconds` 周期。D で tick 基盤ができたら同じ tick stream を消費する形に寄せる (C は polling、D で tick 駆動へ)。
- **config フラグ:** `OrchestratorConfig` に `market_state_enabled: bool = False`。enabled 時のみ state ループ起動 + cadence/regime 接続。

**テスト:** C-1 state 遷移 + ヒステリシス (calm↔active の即時上げ/遅延下げ、閾値バタつき無し、horizon 別閾値)。C-2 active/critical で boost 書込・calm で失効・trade のみ。C-3 regime 変化で planning トリガ + debounce。

**Phase1 実装スコープ (code review 反映):** market state 判定の入力は **Phase1 では価格変化率 (move_pct) ベースに限定**する。detector 自体は `bridge_degraded` / SL-TP 近接 / `important_news` / spread spike も判定に使えるが、Phase1 ではそれらの provider を接続しない:
- **spread:** 現行 quote provider は `spread=None` (bid/ask 非取得)。実 spread は **Phase2/D の websocket tick 基盤**で供給。それまで spread 経路は不活性。
- **position 近接 / bridge:** PriceMonitorWorker (§5.5) / risk_state の責務。market state loop からの接続は Phase2/D 以降。
- **cadence boost 経路②の resolver 共有:** `main._build_cadence_driver()` が生成した `CadenceResolver` を `build_orchestrator_runtime(cadence_resolver=...)` 経由で bridge に渡し実反映する (`cadence_enabled` + `market_state_enabled` 双方 on のとき)。片方 off なら bridge は regime コールバックのみの縮退。
- **regime → planning 再計画:** 現状は regime 変化を log するのみ。planning 再計画への接続は material landing 経由 (§5.4①) を前提とし、push 型の即時トリガは後続。

---

## Phase 2 — D: websocket tick 基盤 + ポジション保護移設 (大・駆動方式刷新)

現状 watch loop は `CurrentPrice` ポーリング (固定 1s)。これを live tick 駆動へ刷新し、本番ポジション保護を同じ tick stream を消費する watch 側へ移設する。**本番ポジション保護に直接影響するため最も慎重に扱う。**

### D-1: quote-stream producer スレッド (§5.5)

- **新規 `src/data/quote_stream.py`:** websocket (MT5 bridge / provider 依存) から live quote (bid/ask/spread/observed_at) を受信し、pair ごとの最新 quote を in-memory に保持する producer スレッド。
- **read-path 2 系統 (§5.5):**
  - watch loop は **live quote を直読** (今この瞬間の価格で plan 条件発火)。
  - planning は従来どおり **decision_snapshot 経由** (時刻整合性)。
- **spread 実値化:** 現行 `make_quote_provider` は spread=None (CurrentPrice に bid/ask 無し)。tick 化で実 spread (bid/ask) が取れるため、`QuoteSnapshot.spread` を実値で埋める。spread gate / freshness wall が実値で効くようになる。
- **watch loop の駆動切替:** `_watch_loop` の固定 1s ポーリングを、tick producer の更新通知駆動 (or 短周期で最新 tick を読む) に変更。**config フラグ `quote_stream_enabled: bool = False`** で切替 (false なら現行ポーリング維持・ロールバック先)。
- **fallback:** websocket 切断時は CurrentPrice ポーリングへ degrade し、freshness wall が stale を検知して trigger を止める (既存安全機構を活用)。

### D-2: ポジション保護移設 (= 旧 E、§4.8 / §5.5)

- 既存 `src/jobs/price_monitor.py` の保護ロジック (`monitor_open_positions` / `position_protection.py` の MFE/R 更新・profit protection・SL/TP 近接・emergency close) を、**同じ live tick stream を消費する watch 側自走スレッド (PriceMonitorWorker)** へ移設する。
- **ロジックは流用 (作り直さない、§4.7):** `compute_mfe_update` / `compute_profit_protection_action` 等の純関数はそのまま。駆動を「5 分ポーリング」から「tick 駆動」へ変えるのが本質。
- **本番影響ゆえの慎重策:**
  - 移設は **shadow→本番の二段**: まず tick 駆動の保護を shadow で並走させ (既存 5 分ポーリングと結果比較)、一致を確認してから既存ポーリングを停止。
  - emergency close は外部副作用なので、移設後も **single execution writer** 原則を守る (保護クローズ経路は 1 つに集約)。
  - **D-1 の tick 基盤が無いと安全に移設できない**ため D-1→D-2 一体。
- **config:** `position_protection_on_tick: bool = False` (既定は既存ポーリング)。

**テスト:** D-1 producer (tick 受信→最新 quote 保持 / 切断時 fallback / spread 実値)。watch loop の tick 駆動切替が freshness と整合。D-2 保護ロジックの tick 駆動 (MFE 更新 / profit protection / emergency close 発火条件) が既存ポーリングと同結果 (並走比較テスト)。

**フェーズ1 とは独立に進められる大物。単独セッション推奨。**

---

## Phase 3 — F: shadow→本番発注 昇格 (大・運用判断事項)

§4.4 broker 結線。現状 PlannerAgent は shadow 境界 (broker に触れない)。F で watch loop の trigger を実発注に繋ぐ。**技術タスクというより、shadow メトリクス (hindsight MFE-R/PnL-R 等) が十分溜まりフェーズ1〜2 が安定してからの踏み切り判断。最後。**

### F-1: 執行段の broker 結線 (§4.4 Option C)

- watch loop が entry 成立を検知 → **RiskGateWorker final gate + broker authoritative gate** を通過した場合のみ broker execution へ進む。
- **既定は決定的・高速執行 (LLM なし)。** material change フラグ時のみ ExecutionOpinionAgent を軽く再点火 (§4.4 Option C、稀)。`execution_recheck_timeout_seconds` 超過時は当該 plan 発火を保留 (安全側)。
- **single execution writer:** 実発注を行う経路は Orchestrator ランタイムの執行段 1 つだけ。`BrokerAdapter.execute_signal` を呼ぶのはこの 1 箇所に集約。

### F-2: durable order lock (§6 / §8.8)

- **`order_intents` テーブル + `plan_id` UNIQUE は既に実装済** (`OrchestratorStore.try_insert_order_intent` / `get_order_intent`)。F で実際の発注経路に組み込む。
- 発注前に `try_insert_order_intent(plan_id, pair, status=pending, owner_run_id, lease_until)` → UNIQUE 違反なら既発注として中止。broker 送信直前に `submitted_at` を埋め、broker 応答で `status` / `order_id` / `broker_result` を更新。
- **クラッシュ復旧 (§8.8 必須):** 起動時 recovery ジョブが TTL 超過 pending を `submitted_at` 有無で判定:
  - `submitted_at` null → `retryable` (未発注、reconciliation 後に再 trigger 可)
  - `submitted_at` あり・`order_id` なし → `needs_reconcile` (broker に問い合わせ必須、確認まで再 trigger 禁止)
  - `submitted_at` あり・`order_id` あり → 正常 (status 補正のみ)
  - 不足する recovery_status カラム / recovery ジョブは F で追加実装 (store には既に owner_run_id/lease_until/submitted_at/recovery_status の余地がある前提、無ければ追加)。

### F-3: broker final gate と二段構え (§4.8)

- RiskGateWorker pre-check (decision 前 reject、LLM コスト節約) + broker final authoritative gate (発注直前)。
- broker `ExecutionResult` を必ず `orchestrator_decisions` に反映 (`order_id` / reject 理由 / `risk_gate_result`)。両 gate の不一致を検出可能にする。

### F-4: 段階導入 (mode 昇格)

- **config:** `OrchestratorConfig.mode` を `shadow` → `live` へ (§12)。`live` 時のみ broker adapter を bootstrap で runtime に渡す。
- **限定発注 (Phase 3 移行 §11):** 限定銘柄・限定時間・小ロット。既存 trading cycle の新規発注は停止 or shadow 降格。
- position management / reconciliation / notifier は既存系を再利用。

**テスト:** F-1 trigger→gate→execute フロー (gate pass で発注 / reject で不発注)。F-2 order_intents (UNIQUE で二重発注防止 / クラッシュ復旧 3 分岐)。F-3 broker reject の decision 反映。F-4 mode=shadow では broker 未結線 (回帰)。

---

## 実装順序とフェーズ境界

| Phase | Task | 内容 | 本番影響 | フラグ (既定) |
|---|---|---|---|---|
| 1 | A | daily summary 配線 + news/event landing 配線 | shadow 内 | `daily_summary_time` / 既存 detector |
| 1 | B | cadence resolver + 可変 interval driver + econ boost | shadow 内 | `cadence_enabled=false` |
| 1 | C | market state 検知 + regime イベント + resolver②接続 | shadow 内 | `market_state_enabled=false` |
| 2 | D | websocket tick 基盤 (D-1) + 保護移設 (D-2) | **本番保護** | `quote_stream_enabled=false` / `position_protection_on_tick=false` |
| 3 | F | shadow→本番発注昇格 (broker 結線 + order lock + 復旧) | **本番発注** | `mode=shadow→live` |

各 Phase 着手時に TDD → code review → commit。Phase 1 は 1 セッションで A→B→C 通し。Phase 2 (D)・Phase 3 (F) は本番影響が大きいため単独セッション + 慎重な段階導入を推奨。

## Review Checklist

- [ ] A: daily summary が 1 日 1 回・時刻跨ぎで発火し、フラグ off / notifier 無で no-op か。
- [ ] A: news/event landing が material フィルタを通り、同一 key 二度発火しないか。
- [ ] B: resolver が most-aggressive-wins + TTL 失効で、watch boost を無視するか。
- [ ] B: driver が skip 時に backfill し (last_run 不更新)、cadence_enabled=false で現行経路を壊さないか。
- [ ] C: state 遷移にヒステリシスが入り、market state が執行を制御しないか。
- [ ] C: regime 変化が debounce 付きで planning をトリガするか (毎遷移で暴発しないか)。
- [ ] D: watch loop が live 価格直読、planning が snapshot 経由の 2 系統を保つか (§5.5)。
- [ ] D: 保護移設が既存ポーリングと並走比較で同結果か、emergency close が single writer か。
- [ ] D: tick 切断時に freshness wall が stale を検知して trigger を止めるか。
- [ ] F: 発注前に RiskGateWorker + broker gate を必ず通るか。
- [ ] F: order_intents の plan_id UNIQUE で二重発注を防ぎ、クラッシュ復旧 3 分岐が動くか。
- [ ] F: mode=shadow では broker 未結線 (回帰) を保つか。
