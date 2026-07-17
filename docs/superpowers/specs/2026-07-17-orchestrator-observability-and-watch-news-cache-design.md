# orchestrator 観測性 + watch news キャッシュ — 設計

**Date:** 2026-07-17
**Status:** 設計確定 (実装前)
**関連:** `2026-06-20-orchestrator-agent-loop-design-v2.md` (設計正本) / consolidated-roadmap §4 (paper 観察項目)
**関連 memory:** [[finance_main_logger_name]] / [[finance_uv_wsl_only]]

---

## 0. 背景 — 稼働ログ調査で判明した 3 問題

paper 稼働中 (Fiosracht, branch `feat/plan-quality-rr`) の `logs/finance.log` / `logs/activity.log` を実測して 3 つの観測性の穴を特定した。3 件とも執行安全性には影響しないが、運用時の可視性を大きく損なう。

### 問題 A: planning が「何を判断したか」がログに出ない

- `logs/finance.log` 全体で `[ORCH]` プレフィックスはわずか 12 行。うち planning 実行を示すのは `📋 plan created` (plan が実際に成立した瞬間) のみ。
- `decision_type` を含む行は **0 件**。`direct_hold` / `reject` / `plan_create` の決定は `OrchestratorStore.record_decision()` で DB に保存されるが、ログには一切出ない。
- 結果、60s ごとに material 判定 ([AGGREGATE] ログ) は回っているのに、その planning cycle が hold したのか reject したのか、なぜ plan を作らなかったのかが**リアルタイムで追えない**。plan 成立時以外は無音。
- さらに `[ORCH]` は `logging_setup.py` の `_PREFIX_REGISTRY` に**未登録**。既存の `[ORCH]` INFO ログは `logs/activity.log` に載らず finance.log にしか出ない。activity.log しか見ない運用では planning が完全に不可視。

### 問題 B: watch loop が 1sec ごとに news 集計して [AGGREGATE] ログを溢れさせる

- 実ログ (2026-07-16 00:08:45〜09:00) で EUR/USD の `[AGGREGATE]` が**毎秒** 1 行出ていた。
- 真因: `_evaluate_plan` / `_evaluate_cf_plan` (runtime.py:543 / 616) が **watch loop の 1sec tick ごとに** `DecisionContextBuilder.assemble()` を呼び、その中の `_build_news` (context_builder.py:170) が `aggregate_news_sentiment` をフル実行する → `news_aggregator.py:83` の INFO ログが毎秒出る。
- `assemble()` の docstring は「毎 tick で snapshot を作らない (軽量化)」を謳うが、**news RAG 集計 (vector store 検索 + 集計) は毎 tick 実行している**。設計意図に反する漏れ。
- EUR/USD だけ毎秒出るのは、EUR/USD に active plan または pending_approval cf plan が保持されており、その plan が生きている限り watch が 1sec 評価するため。plan の無い USD/JPY では出ない (実ログの非対称の正体)。
- news は RSS 30 分更新なので、1sec ごとの再集計は完全に無駄 (計算・ログの両方)。

### 問題 C: planning 中断時の plan 状態 (調査結果・コード変更なし)

ユーザーの「planning を立てている最中にシステムを停止すると plan は削除されるのか」への回答。

- plan 本体は planning cycle の**最後**に commit される (`_commit_plan`, planning_pipeline.py:370)。write 順序は create(requires_replan) → decision → vote → supersede → activate。
- **LLM 応答待ち中に停止** (最も長い区間): plan は保存されず、scan 出力と draft opinion だけ DB に残る。害なし。次回起動で同データから再評価しうる。
- **create_trade_plan 到達後に停止**: `requires_replan` 状態で残るが `get_active_plans()` は active のみ拾うため**執行対象にならない** (安全側)。
- `stop()` は実行中 cycle を強制中断しない (join timeout 2s)。SIGTERM 時は `asyncio.run` 内例外 → `finish_run(status='failed')`。
- **専用回収機構は無い**: 起動時 recovery (`order_recovery.recover_pending_intents`, bootstrap.py:347) は執行段の order_intent 用で、planning 段の requires_replan orphan / dangling run は対象外。ただし執行されないので実害は低い (DB 残留のみ)。

→ **結論: plan は消えない。むしろ requires_replan orphan が残る側だが実害低。今回は注記に留め、回収実装はスコープ外。**

---

## 1. スコープ

| # | 対象 | 実装 |
|---|------|------|
| A | planning cycle 可視化 | runtime + logging_setup |
| B | watch news の short-TTL キャッシュ化 | context_builder (news_provider ラップ) |
| C | orphan 注記 | 本 spec の記録のみ・コード変更なし |

**非スコープ:** requires_replan orphan / dangling run の回収機構 (将来課題)。planning フェーズ内部 (scan/draft/gate) の詳細トレース刷新。watch trigger 判定ロジックの変更。

---

## 2. コンポーネント A — planning cycle 可視化

### 2.1 目的

planning loop (60s cadence) の各 cycle が「回った」ことと「何を決めたか」を activity.log レベルで追えるようにする。

### 2.2 変更

**A-1. `[ORCH]` を `_PREFIX_REGISTRY` に登録** (logging_setup.py)

```python
("[ORCH]",  "bold cyan",  True),   # orchestrator planning / watch / trigger イベント
```

これで既存 + 新規の `[ORCH]` INFO ログが activity.log に載る。着色は他の運用系と重複しないトーンを選ぶ。

**A-2. planning cycle 開始ログ** (runtime.py `run_planning_cycle`)

各 pair の処理開始時に INFO 1 行:

```
[ORCH] planning start: pair=EUR/USD trigger=<cadence|material:news|material:regime|material:event|material:technical>
```

- trigger 種別は detector の material 判定結果から導く。detector 未注入 (後方互換) 経路では `trigger=cadence` 固定。
- material 経路が複数該当する場合は該当する全種別を `material:news+regime` のように連結、あるいは主要 1 種を選ぶ (実装時に detector が既に持つ情報の粒度に合わせる。新たな判定計算は足さない)。

**A-3. planning 決定結果ログ** (planning_pipeline.py / runtime.py)

pipeline が返す `PipelineResult` の決定種別に応じ、cycle 終端で INFO 1 行:

```
[ORCH] planning result: pair=EUR/USD decision=direct_hold reason=<no opportunity|position unavailable|phase1 observe|...>
[ORCH] planning result: pair=EUR/USD decision=reject reason=<scale-in evidence missing|planner reject|revise budget exhausted|risk reject: derived rr ...>
[ORCH] planning result: pair=EUR/USD decision=plan_create plan_id=99 rr=2.10
```

- reason 文字列は既に `record_decision` に渡している `reasoning_summary` を再利用する (新規に組み立てない)。
- plan_create は既存 `📋 plan created` ログがあるので、そこに decision 統一の文言を寄せるか、既存ログを残しつつ result 行を追加するかは実装時に決める (二重に出さない)。

**A-4. フェーズ遷移は DEBUG** (planning_pipeline.py)

scan → draft → gate の遷移ログは **DEBUG** (finance.log のみ)。activity.log を汚さない。詳細デバッグ時だけ見える。今回新設は最小限 (既存の値上書き INFO ログ等はそのまま)。

### 2.3 watch loop は対象外

watch loop は 1sec tick で回るため、ここに cycle ログを足すと activity.log が溢れる。watch 由来の INFO ログは**既存の状態遷移イベントのみ** (shadow trigger / invalidate / cf trigger 等、既に実装済み) に限る。「毎 tick 評価した」ログは出さない。

### 2.4 テスト

- caplog で `run_planning_cycle` が decision 種別ごとに正しい文言・レベルで planning start / result を出すことを検証。
- `[ORCH]` が `_ACTIVITY_PREFIXES` に含まれることを検証 (registry 登録の回帰防止)。

---

## 3. コンポーネント B — watch news の short-TTL キャッシュ

### 3.1 目的

watch loop (1sec) が保持中 plan ごとに毎 tick news をフル集計するのを止める。news は RSS 30 分更新なので、短い TTL キャッシュで十分。計算 (vector store 検索 + 集計) とログの両方を根絶する。

### 3.2 設計

`make_news_provider` (context_builder.py:399) が返す `NewsProvider` を **TTL キャッシュでラップ**する。

```python
def make_cached_news_provider(
    inner: NewsProvider, *, ttl_seconds: float, clock: Callable[[], datetime],
) -> NewsProvider:
    """pair 単位で (value, fetched_at) を保持し、TTL 内は再集計せず返す。"""
    cache: dict[str, tuple[dict, datetime]] = {}
    def provider(pair: str) -> dict:
        now = clock()
        hit = cache.get(pair)
        if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
            return hit[0]
        value = inner(pair)          # ← ここでのみ aggregate_news_sentiment が走る
        cache[pair] = (value, now)
        return value
    return provider
```

- **キャッシュキー = pair**。値 = §7 news ブロック dict (`sentiment_score` / `confidence` / `top_reasons`)。
- **TTL 既定 = 60s** (config 化: `OrchestratorConfig.entry.news_cache_ttl_seconds: float = 60.0`。`__post_init__` で有限 > 0 を検証。RSS 30 分更新に対し十分保守的)。
- **clock は注入** (db_now を渡す)。テストで時間を進められるよう関数引数にする。Date.now 直呼びしない。
- ラップ位置は bootstrap で `make_news_provider` の戻りを `make_cached_news_provider` で包む 1 箇所。`DecisionContextBuilder` に渡る provider がキャッシュ済みになる。

### 3.3 build 経路との関係

- `build()` (planning, snapshot materialize) も同じ provider を使う。planning は 60s cadence なので TTL 60s ならほぼ毎回 miss して新鮮な値を取る (planning material 判定の精度は維持)。
- watch (1sec) は TTL 内で hit し続け、60s に 1 回だけ実集計。→ [AGGREGATE] ログは pair あたり最大 60s に 1 回に減る。
- material landing 経路 (`make_news_material_provider`, landing_providers.py) は planning loop の material 判定で使われ、`make_news_provider` とは別の provider を返す (戻り値が dict でなく NewsSentiment)。§3.4 で別途キャッシュする。

### 3.4 B-2: material landing 経路の二重集計

`make_news_material_provider` の `get_news_impact` + `get_news_key` が 1 回の material 判定で各々 `_aggregate` を呼び、同 pair を 2 回集計する (impact≥閾値時)。加えて `commit_seen` でも `get_news_key` を呼ぶため 1 判定で最大 3 回集計しうる。

- **対応:** `make_news_material_provider` 内の `_aggregate(pair)` を同一 TTL キャッシュで包む。§3.2 のラッパは NewsProvider (dict 返し) 用なので、`_aggregate` (NewsSentiment 返し) 用に **同じ TTL ロジックを共有する内部ヘルパ**を切り出して両者から使う (キャッシュ実体は関数ごとに別。共有するのは「pair→(value, fetched_at) を TTL 判定するロジック」)。
- TTL・clock は §3.2 と同じ `news_cache_ttl_seconds` / db_now を使う。
- これで planning material 判定由来の [AGGREGATE] (60s 間隔・EUR/USD 2〜3 行) も 1 行に減る。
- **キャッシュ実体を 2 つ持つことの整合:** watch 経路 (dict provider) と material 経路 (NewsSentiment) は別プロセス位置・別頻度で呼ばれるため、キャッシュを共有せず各々が独立に最大 TTL 秒だけ古い値を返す。値のズレは最大 TTL 秒で、判断品質に影響しない (news は 30 分粒度)。

### 3.5 テスト

- 同一 pair を TTL 内で複数回呼ぶと inner (aggregate) が 1 回しか呼ばれないことを mock で検証。
- TTL 超過後は再集計されることを clock を進めて検証。
- material provider の impact + key 連続呼び出しで aggregate が 1 回に集約されることを検証。

---

## 4. コンポーネント C — orphan 注記 (コード変更なし)

§0 問題 C の調査結果を本 spec の記録として残す。実装は将来課題。

**将来検討 (今回スコープ外):**
- 起動時に `status='requires_replan'` かつ古い plan を expired に掃く reconcile。
- `finished_at IS NULL` かつ古い run を failed に確定する掃除。

いずれも執行安全性には無関係 (執行されない) なので優先度低。DB 肥大 / 一覧クエリのノイズが実害の上限。

---

## 5. データフロー (変更後)

```
planning loop (60s)
  run_planning_cycle
    → [ORCH] planning start: pair trigger    (A-2, INFO, activity)
    → detector material 判定 (cached news_provider 経由で集計 1 回)
    → pipeline.run → PipelineResult
    → [ORCH] planning result: decision reason (A-3, INFO, activity)
    → (scan/draft/gate 遷移は DEBUG, finance.log のみ)   (A-4)

watch loop (1sec)
  run_watch_cycle → 各 active/cf plan
    → assemble → _build_news → cached news_provider
        TTL 内: cache hit (集計せず・ログ出ず)         (B)
        TTL 超過 (60s に 1 回): 実集計 + [AGGREGATE] 1 行
    → 状態遷移時のみ既存の [ORCH] shadow trigger / invalidate 等
```

---

## 6. 影響範囲・非互換

- **挙動不変性:** news キャッシュは値を最大 TTL 秒古くするだけ。watch の news_conflict 判定は 60s 粒度で更新 (現状の毎秒判定は過剰)。planning material 判定は 60s cadence と同期するので実質不変。
- **ログ増減:** activity.log に planning start/result が増える (60s ごと数行) が、watch 由来 [AGGREGATE] が pair あたり毎秒 → 60s に 1 回へ激減。差引で activity.log は読みやすくなる。
- **config 追加:** `news_cache_ttl_seconds` (既定 60s)。未設定時は既定で挙動する (後方互換)。
- **`[ORCH]` registry 登録**は既存 `[ORCH]` ログ (shadow trigger 等) も activity.log に載せる副作用がある。これは可視性向上として許容 (元々 finance.log には出ていた)。

---

## 7. 実装順 (概略・詳細は plan で)

1. logging_setup に `[ORCH]` 登録 + テスト (A-1)
2. news TTL キャッシュ ラッパ + bootstrap 配線 + テスト (B, B-2)
3. planning start / result ログ + テスト (A-2, A-3)
4. フェーズ遷移 DEBUG 整理 (A-4)

各ステップ TDD (RED → GREEN)。finance の uv/pytest は WSL 内実行厳守 ([[finance_uv_wsl_only]])。
