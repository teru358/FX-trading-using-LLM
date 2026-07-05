# Day Horizon 移行 (swing→day スケール縮小) — Design Spec

**Date:** 2026-07-05
**Status:** Draft — codex レビュー 1 巡目 (High×3 / Med×3 / Low×1) 反映済み (2026-07-05)。High#1→S-4b、High#2→S-4a、High#3→S-4c、Med#1→S-1、Med#2→S-5、Med#3→V-1 スコープ確定 (query filter は YAGNI で見送り)、Low→D-1 修正
**Scope:** finance project — live_test 移行に伴い、運用 horizon を swing から day へ切り替える。スケール縮小の第一次値で観察を開始し、実測 (ATR/spread 分布) で微調整する。
**親文脈:** `2026-07-04-consolidated-roadmap.md` Phase 2-2。設計正本 = `2026-06-20-orchestrator-agent-loop-design-v2.md` §4.6 (trade_horizon)。
**数値の導出根拠:** 2026-07-05 ブレスト — ①無次元比の保存 (swing 設定の内部比率を固定し時間スケールを約 1/4〜1/6 に縮小) ②セッション長 (~8h) アンカー ③スプレッド経済学 (TP に対する spread 比率 < 5%)。

---

## 1. Goal

1. orchestrator (version2) の運用 horizon を day に切り替え、執行サンプル蓄積を週 1-2 → 週 10-30 ペースへ引き上げる (検証速度の向上が主目的)。
2. day の実挙動に必要な時間スケール群 (SL/TP 距離・plan TTL・鮮度・ポジション管理タイマー・planning cadence・hindsight 窓) を一貫した縮小比で設定する。
3. day 検証データを swing 期データと分離し (horizon タグ)、5 問題スコアカードを `horizon='day'` で集計可能にする。

## 2. Non-goals

- **day_profile / swing_profile の切替オーバーレイ機構は作らない。** 設定値を直接 day 値へ変更する (単一基準哲学)。swing⇔day の往復運用需要が出たら別 spec で機構化。
- **`client.py horizon` コマンド / `POST /orchestrator/horizon` は実装しない** (片道切替のため config 編集で足りる。spec v2 §4.6 の該当部分は保留継続)。
- technical LLM 分析のオミット (別項 — roadmap Phase 2-1。本 spec とは独立だが順序依存あり、§7)。
- 承認ゲート / forecast 退役 / C' (それぞれ別項)。
- scalp 対応 (spec v2 で除外済み — 本 spec の 15m は執行足であり、SL/TP 基準は 1h に留める。§4 D-5)。
- watch instrument の収集変更 (yfinance / 120h staleness / base interval 据え置き)。

## 3. 前提条件 (本 spec の実施前に満たすこと)

1. **live_test 切替完了** (bridge DRY_RUN=true → stick/Fiosracht 両系 live_test 化 → `diag_planner_hold.py F` で FX stale→ok 確認)。15m 足と 90 分鮮度は MT5 データ経路なしでは成立しない (yfinance FX ~7h 遅延)。
2. `tick_migration_stage=producer` 以上 (spread=None 全 reject 回避、M3)。
3. plan 品質バグ修正済みが望ましい (SchemaParseError / RR 計算ミス — roadmap Phase 1-1。未修正のまま day 化すると reject ノイズで観察が汚れる)。

## 4. 設計判断

| # | 判断 | 内容 |
|---|---|---|
| D-1 | **直接設定値変更方式** | オーバーレイ機構を作らない。**schema 既定値は swing 互換のまま維持し、day 値は settings.yaml でのみ与える** (コード変更とday値適用を分離し、既定値=挙動不変を保証)。`policy.trade_horizon: "day"` は trace 列・market_state overlay・context 伝搬のために切り替える (既存配線が動く) |
| D-2 | **plan TTL は LLM 出力 + 決定的クランプ** | ExecutionOpinion の expires_at は LLM が決めるが、**新設 config `plan_ttl_max_hours` で上限クランプ** (超過時は now+clamp に切り詰めて log)。LLM 出力を信用しない原則の決定的バックストップ (confidence と同型) |
| D-3 | **staleness の config 化** | `_MAX_STALENESS_FX` (technical_collector.py:54 ハードコード定数) を config へ移す。horizon 依存値になった時点で定数では管理不能 |
| D-4 | **収集 interval の分粒度対応** | `technical_trade_interval_hours: int` では 30 分を表現できない。`technical_trade_interval_minutes: int \| None = None` を追加し、設定時は hours より優先 (未設定なら現行挙動不変) |
| D-5 | **SL/TP 基準足は 1h で止める (15m にしない)** | 15m ATR 基準だと TP 15-40pips で spread 比率が 10% 超に悪化し scalp の経済構造に入る。15m は執行足 (条件評価・パターン)、SL/TP は 1h ATR — spec v2 §4.6 の「ATR timeframe と entry 感度は別項目」と整合 |
| D-6 | **RR=2.0 不変** | TP/SL 倍率比は swing (6/3) と同じ 2.0 を維持 (4/2)。day で変えるのは距離であってリスク哲学ではない |
| D-7 | **hindsight 窓 = time_stop 整合** | 評価窓 (8h) を実際の出口ポリシー (stale_position_review 8h) に合わせる。「運用では 8h で強制クローズするのに採点は 24h 後の TP を勝ちと数える」乖離を防ぐ |
| D-8 | **hindsight は spread 込み採点** | day は spread の利益比率が swing の 2-3 倍。trigger 時に記録済みの spread を R 換算で pnl_r から控除しないとスコアカードが実態より甘くなる |
| D-9 | **MTF は bar 本数保存でシフト** | 各 TF の lookback を bar 本数がほぼ同じになるよう縮小 (indicator の統計的性質を維持) |

## 5. 変更一覧

### 5.1 構造変更 (コード変更を伴う 7 件 — 2026-07-05 codex レビューで S-4 を 3 分割・対象拡大)

| # | 変更 | 対象 | 内容 |
|---|---|---|---|
| S-1 | **plan TTL クランプ** | `planning_pipeline.py` + `OrchestratorConfig` | 新 config `plan_ttl_max_hours: int = 0` (schema 既定 0 = クランプ無効 = 挙動不変、day yaml で 8 — D-1 と整合)。draft 受領時に `expires_at > now + clamp` なら切り詰め + INFO log。expires_at 欠落時の既存挙動 (構造 reject) は不変。**datetime 正規化必須 (codex Med#1):** LLM 出力は `+00:00` 付きなら aware になる ([schemas.py:257]) が DB 規約は naive local ([clock.py db_now])。比較前に `to_db_naive_datetime()` で正規化し、**クランプ後の値を draft に反映してから** opinion 保存 ([planning_pipeline.py:152]) と plan 保存 ([:250]) の両方に流す (保存経路間の不一致を作らない) |
| S-2 | **プロンプトの horizon 指針** | `execution_opinion_agent.py` / `planner_agent.py` | 現状プロンプトは horizon 無言及。day 時の指針を追加: 「plan は数時間で完結する day trade。expires_at は最長 {plan_ttl_max_hours}h。entry 条件は現在価格から到達可能な距離 (1h ATR の 0.3-1.5 倍目安)。RR ≥ 2 を維持」。trade_horizon は context に既に流れているため、プロンプトテンプレート側で分岐 |
| S-3 | **staleness config 化** | `technical_collector.py` + schema | `_MAX_STALENESS_FX` → `technical_max_staleness_fx_minutes: int = 360` (既定は現行 6h と等価 = 挙動不変)。day 設定で 90 へ。watch 側 (120h) は定数のまま |
| S-4a | **PriceStore の interval 対応 (codex High#2)** | `price_store.py` / `mt5_ohlcv_fetcher.py` | 現行 PK は `(symbol, bar_time)` のみで 1h/15m が同一テーブルに混在してしまう → **`interval` 列を PK に追加**。OHLCV は再構築可能な cache のため既存テーブルは migration ではなく再構築 (drop→再フェッチ) で移行。差分フェッチの `latest + timedelta(hours=1)` 固定 ([mt5_ohlcv_fetcher.py:194]) を **interval 刻みにパラメータ化** (15m で直近 45 分を取り逃がさない)。interval キー共存により **rollback = config 編集のみ** が成立する |
| S-4b | **MTF の 15m 対応 (codex High#1)** | `resample.py` / `mtf.py` | ① `_RULE_MAP` に `15m`/`30m` を追加 ([resample.py:11] 現状 15m で ValueError)。② `resample_ohlcv` の「入力=1h」前提を「入力=基底足 (config の ohlcv_interval)」にパラメータ化 (15m 基底から 1h/4h を合成)。③ `_bars_per_day_for_interval` に `Nm` 形式対応 ([mtf.py:166] 現状 1d/Nh のみ → 15m=96 本/日)。④ MTF テスト追加 |
| S-4c | **スケジューラ/cadence の分粒度配線 (codex High#3)** | `technical_schedule.py` / `main.py` / `ScheduleConfig` + loader | `technical_trade_interval_minutes: int \| None = None` 追加 (設定時は hours より優先、未設定なら挙動不変)。配線 3 箇所: ① `technical_times_for` を分粒度対応 ("HH:MM" リスト生成、[technical_schedule.py:7] 現状 hour 単位) ② main の union dispatch ([main.py:239] `technical_trade_interval_hours` 直参照) ③ cadence base ([main.py:102] hours×3600) を minutes 優先で算出。loader の field 列挙漏れに注意 (`execution_opinion_recheck_enabled` の前例) + テスト |
| S-5 | **hindsight spread 込み採点** | `hindsight_evaluator.py` / `orchestrator_store.py` | trigger 時の spread (producer stage で取得済み) を R 換算し pnl_r から控除。`spread_cost_r` を評価行に別カラム保存。**DB migration 必須 (codex Med#2):** `shadow_hindsight_evaluations` に列が無く orchestrator_store は `_migrate()` を持たない → **analysis_store の `_migrate` (ALTER TABLE・冪等) パターンを移植**して列追加。ORM field + `update_hindsight_evaluation()` シグネチャ + shadow notifier の hindsight 表示に spread_cost_r を 1 項目追加 |

### 5.2 設定値変更 (yaml / 既定値のみ)

| 設定 | 現行 (swing) | 新 (day) | 根拠 (要約) |
|---|---|---|---|
| `orchestrator.policy.trade_horizon` | swing | **day** | trace / market_state overlay / context に波及 |
| `trading.atr_timeframe` | 4h | **1h** | √時間スケール、D-5 |
| `trading.sl_atr_mult_default` | 3.0 | **2.0** | SL 距離 ≈ swing の 1/3 (ATR 半減 × 倍率 2/3)。spread 余裕を見て 1.5-2.0 の上端 |
| `trading.tp_atr_mult_default` | 6.0 | **4.0** | RR=2.0 不変 (D-6)。TP≈30-60pips → spread 比率 1-4% |
| `analysis.multi_timeframe.long` | 1d / 90d | **4h / 15d** | bar 本数 ~90 本を保存 (D-9)。趨勢 = horizon の 2 段上 |
| `analysis.multi_timeframe.medium` | 4h / 14d | **1h / 4d** | bar 本数 ~84→96 本 |
| `analysis.multi_timeframe.short` | 1h / 2d | **15m / 1d** | bar 本数 48→96 本。執行足 |
| `trading.ohlcv_interval` | 1h | **15m** | S-4 基底足 |
| `technical_max_staleness_fx_minutes` (S-3 新設) | (6h 定数) | **90** | 最短足 6 本分 = 収集 interval×3。≥ interval×2 が flap 回避の安全条件 |
| `orchestrator.entry.max_technical_age_seconds` | 1800 | **5400** | staleness (90min) と整合。収集 30min 周期で 1800s のままだと watch loop gate が flap する |
| `technical_trade_interval_minutes` (S-4 新設) | (1h) | **30** | 15m 足 2 本分。LLM slot 負荷は §7 参照 |
| `orchestrator.firing.min_planning_interval_seconds` | 1800 | **900** | 収集 interval の半分 (現行比率を保存)。§7 の starvation 監視付き |
| `orchestrator.hindsight.horizon_seconds` | 86400 | **28800** (8h) | D-7: time_stop 整合。当日中に採点確定 |
| `trading.no_progress_watch_hours` / `exit_hours` | 6 / 12 | **2 / 4** | time_stop の 25% / 50% 比率を保存 |
| `trading.stale_position_review_hours` (time_stop 実体) | 24 | **8** | セッション長アンカー。オーバーナイト回避 |
| `trading.timeout_cooldown_hours` | 4 | **1** | 1/4 スケール |
| `trading.stale_signal_hours` | 8 | **2** | 1/4 スケール |
| `trading.reversal_min_holding_minutes` | 240 | **60** | 1/4 スケール |
| `orchestrator.plan_ttl_max_hours` (S-1 新設) | — | **8** | 1 セッション上限。下限側は planning cycle (15min) の数十倍を確保 |

**変更しないもの (明示):** profit protection 閾値 (R 基準で horizon 非依存 — 観察して問題があれば)、market_state の各 seconds / spread_spike_pips (day overlay は active_move_pct×0.5 が既存配線で自動適用)、debounce_window_seconds=180、locks TTL、watch 系全部、`session_end_flatten_enabled=false` (新しい安全装置を足さない — time_stop 8h で実質カバー)。

### 5.3 データ・検証整合

| # | 作業 | 内容 |
|---|---|---|
| V-1 | RAG case card の horizon タグ (**write 側のみ今回実装** — codex Med#3 受けスコープ確定) | **今回:** `directional_writer.py` の各 record_* 関数に horizon param を追加し新規カード metadata に `horizon` を書く (day 初日からラベル正解)。「horizon キー無し = legacy swing」を規約として確定。**今回やらない:** `DirectionalStore.query()` の horizon フィルタ (phase と `$and` 合成が必要) — 現状 orchestrator の similar_cases は空固定 ([context_builder.py:147]) で読者が存在せず、旧経路 rag_adjustment は削除予定のため YAGNI。similar_cases 結線時 (別項) に query filter + legacy 表示を実装 |
| V-2 | スコアカード集計の horizon 固定 | 5 問題スコアカード / 較正 / 卒業判定のクエリを `WHERE trade_horizon='day'` に。swing 値は対照群としてのみ参照 |
| V-3 | reflection ATR 係数の持ち込み禁止 | swing 期に学習された sl/tp_atr_mult 調整値が day 初期値に混入しないことを確認 (reflection 提案の適用先と永続化の有無を要確認)。day は 2.0/4.0 から再スタート |
| V-4 | `recent_trade_stats.window_hours` の day 値 | spec v2 §7 は horizon 連動 (day=短い窓) を指定。現実装の窓を確認し、day 用に 24h へ (swing 相当値より短縮) |

## 6. 移行手順

1. 前提条件 (§3) を満たす。
2. S-1〜S-5 実装 (TDD、既定値は挙動不変に保つ — S-3 の既定 360min、S-4 の minutes=None fallback)。
3. settings.yaml に §5.2 の day 値を一括適用 + `trade_horizon: day`。
4. Fiosracht (live_test) で観察開始。§7 の観察項目を N 営業日 (consolidated-roadmap の比較期間に従う)。
5. **実測検算 (切替後早期に 1 回):** MT5 データで USDJPY/EURUSD の 1h/15m ATR 分布と実 spread 分布を集計し、「spread / TP 距離 < 5%」を確認。崩れていれば sl/tp_atr_mult を上方調整 (RR=2 は維持)。
6. **rollback:** 全て config 編集で swing 値へ戻せる (機構なしの利点)。OHLCV cache は S-4a の interval 列により 1h/15m がキー共存するため、戻しても cache 汚染・取り直し不整合は起きない。day 期の判断データは horizon タグで分離済みのため汚染なし。

## 7. 観察項目・リスク

| 観点 | 見るもの | 悪化時の対処 |
|---|---|---|
| **LLM slot starvation** | planning floor 900s + 収集 30min で skip/backfill 頻度 (`data_freshness_snapshots`)。**technical LLM オミット (Phase 2-1) 未実施の間が最も危険** | `min_planning_interval_seconds` を 1800 へ戻す (config 一発)。または Phase 2-1 を先行させる |
| staleness flap | stale↔ok の振動、suspended 落ち頻度 | staleness 90→120min へ緩和 |
| plan triggered rate | 両端監視 (spec v2 §11 metric 2)。TTL 8h で「立てては失効」が多発しないか | TTL / entry 条件距離のプロンプト指針を調整 |
| spread 経済性 | hindsight の spread_cost_r 分布 (S-5 で可視化) | sl/tp_atr_mult 上方調整 |
| claude-cli 429 | planning 頻度上昇で発生率が上がる可能性 (既知の残課題) | 発生率次第でレート制御/フォールバック対策の優先度繰上げ |
| 静かすぎる障害 | plan 0 件 / trigger 0 件の日次検知 (TZ バグ 19h 無発注の前例) | daily summary に plan/trigger 件数を必須表示 |

## 8. Open Questions

1. ~~S-4 の 15m 合成: finance 側 resample か bridge 15m endpoint か~~ → **finance 側 resample で確定** (S-4b。bridge 無改修)。
2. V-3: reflection の ATR 係数提案が永続化されているか (されていなければ作業不要)。
3. 観察期間 N 営業日の値 (consolidated-roadmap の比較期間 Open Q と共通)。
4. 承認ゲート (Phase 2-3) 導入時: day TTL 8h に対する承認レイテンシ → veto 窓方式の要否 (roadmap §1.2 の 3 段階のうち②)。

## 9. Review Checklist (実装 plan 作成前)

- [ ] S-3/S-4 の新 config が既定値で**挙動不変** (day 値は settings.yaml でのみ有効化) になっているか。
- [ ] plan TTL クランプが「切り詰め + log」で、reject にしていないか (plan を殺すのは expiry の仕事)。
- [ ] プロンプト指針 (S-2) が trade_horizon で分岐し、swing に戻した時に swing 指針へ戻るか。
- [ ] hindsight spread 控除 (S-5) が swing 期の既存評価行を再計算しない (day 以降の新規評価のみ) か。
- [ ] 15m 基底足化 (S-4) が watch 経路 (yfinance) に影響しないか。
- [ ] `max_technical_age_seconds` (5400) ≥ staleness (90min) ≥ 収集 interval (30min)×2 の不等式が保たれているか。
- [ ] time_stop (8h) = hindsight 窓 (8h) の整合が取れているか。
- [ ] V-1 の読み側フィルタが「horizon キー無し」を legacy として扱い、既存 ChromaDB を書き換えないか。
- [ ] rollback が config 編集のみで完結するか (コード側に day 分岐のハードコードを作っていないか)。
- [ ] スコアカード / 卒業判定クエリが horizon 混在集計になっていないか (V-2)。
- [ ] **(codex 2巡目反映)** TTL クランプの datetime 正規化 (aware `+00:00` → naive local) がテストされ、クランプ後値が opinion 保存と plan 保存の両方に流れているか (S-1)。
- [ ] **(codex 2巡目反映)** PriceStore interval 列追加後、旧 cache の再構築手順と「swing へ戻しても interval キー共存で汚染しない」ことが確認されているか (S-4a)。
- [ ] **(codex 2巡目反映)** resample `_RULE_MAP` 15m/30m + 基底足パラメータ化 + `_bars_per_day` の Nm 対応にテストがあるか (S-4b)。
- [ ] **(codex 2巡目反映)** 分粒度スケジュール ("HH:MM" リスト) / main dispatch / cadence base の 3 配線 + loader 列挙にテストがあるか (S-4c)。
- [ ] **(codex 2巡目反映)** `spread_cost_r` の ALTER TABLE migration が冪等か (analysis_store `_migrate` パターン準拠) (S-5)。
