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
- `stop()` は実行中 cycle を強制中断しない (join timeout 2s でスレッド参照を外すだけ・runtime.py:1325)。**`main.py` に SIGTERM handler は無く** (main.py:579)、graceful shutdown は主に `KeyboardInterrupt` + `finally` 依存。したがって **`finish_run(failed)` の確定は保証されない**: SIGTERM や 2 秒を超える LLM 処理で kill された場合、`finished_at IS NULL` の run が残り得る (dangling run)。正常な例外経路 (planning_pipeline の fail-safe / runtime の try-except) では failed 確定するが、プロセス強制終了はそれを保証しない。
- **専用回収機構は無い**: 起動時 recovery (`order_recovery.recover_pending_intents`, bootstrap.py:347) は執行段の order_intent 用で、planning 段の requires_replan orphan / dangling run は対象外。ただし執行されないので実害は低い (DB 残留のみ)。

→ **結論: plan は消えない。むしろ requires_replan orphan / dangling run が残る側だが実害低 (執行されない)。今回は注記に留め、回収実装はスコープ外。**

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
[ORCH] planning start: pair=EUR/USD trigger=news+regime
```

- trigger 種別は detector から取得する。**現状 `pairs_to_plan()` は pair list しか返さず、trigger 理由は取れない** (material_landing.py:214)。かつ `is_material()` は短絡評価 (material_landing.py:162) のため、事後に再判定しても全該当理由を正確に取れず news 集計の再実行にもなる。→ **detector API を変更する** (A-5 参照)。
- detector 未注入 (後方互換) 経路では `trigger=cadence` 固定。

**A-3. planning 決定結果ログ + PipelineResult データ契約** (planning_pipeline.py / runtime.py)

cycle 終端で決定種別に応じ INFO 1 行:

```
[ORCH] planning result: pair=EUR/USD decision=direct_hold reason=no opportunity
[ORCH] planning result: pair=EUR/USD decision=reject reason=risk reject: derived rr 1.20 below min 1.50
[ORCH] planning result: pair=EUR/USD decision=plan_create plan_id=99 rr=2.10
```

**データ契約 (High 指摘対応):** ログの正本データを `PipelineResult` に集約する。現状は不足があり、そのままでは正確なログを組めない:

- `direct_hold` 経路 (planning_pipeline.py:154/177) は `PipelineResult.reason` を設定していない。
- `risk reject` の `PipelineResult.reason` は risk gate 由来で、DB に保存する `reasoning_summary` (planner 理由) と別物 (planning_pipeline.py:331 付近)。
- `plan_create` のログに出す RR は現在 `PipelineResult` に無い (planning_pipeline.py:414)。
- `phase1 observe` 経路 (runtime.py:264) は pipeline 未注入で `PipelineResult` 自体が無い。

**対応:**

1. `PipelineResult` に **`reason: str`** (全 outcome で必ず設定・ログ用の正本) と **`derived_rr: float | None`** (plan_create/reject で設定) を持たせる。
2. **全 return 経路** (failed / direct_hold ×2 / reject ×N / plan_create) で `reason` を明示設定する。既存の `reasoning_summary` と揃える (direct_hold は「no opportunity」「position/current_plan unavailable」等、reject は planner/risk それぞれの理由)。
3. `reason` は**改行除去 + 長さ制限** (例: 200 字で truncate)。ログ 1 行を壊さない。
4. **phase1 observe** (pipeline 未注入) は `PipelineResult` を経由しないため、runtime.py:264 の分岐で直接 `[ORCH] planning result: ... decision=direct_hold reason=phase1 observe` を出す。
5. plan_create は既存の可視ログ (`📋 plan created`) と二重に出さない。既存ログを result 契約に寄せるか、既存を残して result 行だけ追加するかは実装時に統一 (どちらか一方)。
6. ログ出力は既存の `_notify_planning_result(pair, result)` (runtime.py:263) と同じ位置で行い、通知とログの正本を一元化する (再 query しない)。

**A-5. detector API 変更 — trigger 理由の受け渡し** (material_landing.py)

`pairs_to_plan()` の戻り値を pair list から **`list[PlanningTarget]`** に変更する:

```python
@dataclass(frozen=True)
class PlanningTarget:
    pair: str
    triggers: tuple[str, ...]   # ("news", "regime") 等・空なら cadence
```

- **発火判定と理由生成を同一評価内で確定する** (短絡評価をやめ、該当した全経路を集める)。事後の再判定 (news 再集計を招く) を避けるのが主目的。
- runtime は `target.pair` / `target.triggers` を使い planning start ログの trigger 種別を組む。triggers が空 = cadence floor 起因。
- 既存呼び出し側 (runtime.py:219) を `PlanningTarget` 消費に合わせる。detector 未注入経路 (後方互換) は従来通り全 pair・trigger=cadence 扱い。

**A-4. フェーズ遷移は DEBUG** (planning_pipeline.py)

scan → draft → gate の遷移ログは **DEBUG** (finance.log のみ)。activity.log を汚さない。詳細デバッグ時だけ見える。今回新設は最小限 (既存の値上書き INFO ログ等はそのまま)。

### 2.3 watch loop は対象外

watch loop は 1sec tick で回るため、ここに cycle ログを足すと activity.log が溢れる。watch 由来の INFO ログは**既存の状態遷移イベントのみ** (shadow trigger / invalidate / cf trigger 等、既に実装済み) に限る。「毎 tick 評価した」ログは出さない。

### 2.4 テスト

- caplog で `run_planning_cycle` が decision 種別ごと (direct_hold / reject / plan_create / failed / phase1 observe) に正しい文言・レベルで planning start / result を出すことを検証。
- `PipelineResult.reason` が全 return 経路で non-None・改行なし・長さ制限内であることを検証。
- `PipelineResult.derived_rr` が plan_create/reject で設定されることを検証。
- `pairs_to_plan` が `PlanningTarget(pair, triggers)` を返し、複数 material 該当時に全 trigger を含むことを検証 (短絡しない)。
- trigger 理由生成で news 集計が余分に走らないことを検証 (aggregate 呼び出し回数)。
- `[ORCH]` が `_ACTIVITY_PREFIXES` に含まれることを検証 (registry 登録の回帰防止)。

---

## 3. コンポーネント B — watch news の short-TTL キャッシュ

### 3.1 目的

watch loop (1sec) が保持中 plan ごとに毎 tick news をフル集計するのを止める。news は RSS 30 分更新なので、短い TTL キャッシュで十分。計算 (vector store 検索 + 集計) とログの両方を根絶する。

### 3.2 設計

`make_news_provider` (context_builder.py:399) が返す `NewsProvider` を **TTL キャッシュでラップ**する。

**並行実行と失敗時の設計 (Medium 指摘対応):** planning loop と watch loop は**別スレッド**で同時に動き、同じ context builder / news_provider を共有する (runtime.py:1297 でスレッド起動)。単純 dict キャッシュだと (a) TTL 境界で両スレッドが同時 miss して二重集計する、(b) inner が例外だとキャッシュされず `_build_news` (context_builder.py:269) が毎秒例外を記録して再試行する — `[ORCH]` を activity 対象にするため障害時に新たなログ洪水になる。→ **pair 単位 lock (single-flight) + negative cache** を仕様に含める。

```python
def make_cached_news_provider(
    inner: NewsProvider, *, ttl_seconds: float,
    negative_ttl_seconds: float, clock: Callable[[], datetime],
) -> NewsProvider:
    """pair 単位で (value, fetched_at) を保持し、TTL 内は再集計せず返す。
    pair 単位 lock で single-flight (同時 miss の二重集計を防ぐ)。
    inner 例外は negative cache し、短時間は再集計/再ログしない。"""
    cache: dict[str, tuple[dict, datetime]] = {}
    neg: dict[str, datetime] = {}                 # pair -> 直近失敗時刻
    locks: dict[str, Lock] = defaultdict(Lock)    # pair 単位
    guard = Lock()                                # locks/neg 辞書自体の保護

    def provider(pair: str) -> dict:
        now = clock()
        hit = cache.get(pair)
        if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
            return hit[0]
        with guard:
            lock = locks[pair]
        with lock:                                # single-flight: 同 pair は 1 本
            hit = cache.get(pair)                 # lock 取得後に再チェック (先行者が埋めた)
            if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
                return hit[0]
            failed_at = neg.get(pair)
            if failed_at is not None and (now - failed_at).total_seconds() < negative_ttl_seconds:
                return DecisionContextBuilder._empty_news()   # 失敗を short-cache・再集計しない
            try:
                value = inner(pair)               # ← ここでのみ aggregate_news_sentiment が走る
            except Exception:
                neg[pair] = now
                logger.warning("[ORCH] news aggregate failed for %s — negative-cached", pair)
                return DecisionContextBuilder._empty_news()
            cache[pair] = (value, now)
            neg.pop(pair, None)
            return value
    return provider
```

- **キャッシュキー = pair**。値 = §7 news ブロック dict (`sentiment_score` / `confidence` / `top_reasons`)。
- **成功 TTL 既定 = 60s** / **negative TTL 既定 = 30s** (config 化: `OrchestratorConfig.entry.news_cache_ttl_seconds: float = 60.0` / `news_cache_negative_ttl_seconds: float = 30.0`。`__post_init__` で有限 > 0 を検証)。
- **例外は provider 内で握って negative cache 化**する。inner (`make_news_provider`) 側は従来通り集計するが、`_build_news` の毎秒例外ループは cache 層で断つ。`_build_news` 側の既存 try/except (context_builder.py:269) はそのまま (二重防御)。
- **clock は注入** (db_now を渡す)。テストで時間を進められるよう関数引数にする。Date.now 直呼びしない。
- ラップ位置は bootstrap で `make_news_provider` の戻りを `make_cached_news_provider` で包む 1 箇所。`DecisionContextBuilder` に渡る provider がキャッシュ済みになる。

### 3.3 build 経路との関係

- `build()` (planning, snapshot materialize) も同じ provider を使う。planning は 60s cadence なので TTL 60s ならほぼ毎回 miss して新鮮な値を取る (planning material 判定の精度は維持)。planning cycle 由来の二重集計 (7/17 ログの EUR/USD 60s 2 行) は build 経路でなく material landing 経路 (§3.4) 由来なので、そちらのキャッシュで解消する。
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
- material provider の impact + key (+commit_seen) 連続呼び出しで aggregate が 1 回に集約されることを検証。
- **並行テスト**: 2 スレッドが同時に同 pair を miss しても inner が 1 回しか呼ばれない (single-flight)。
- **失敗テスト**: inner が例外を投げても TTL(negative) 内で 1 回しか呼ばれない・provider は空 news を返す・例外ログが毎回出ない (negative cache)。

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
- **config 追加:** `news_cache_ttl_seconds` (既定 60s) / `news_cache_negative_ttl_seconds` (既定 30s)。未設定時は既定で挙動する (後方互換)。
- **detector 戻り値変更:** `pairs_to_plan` の戻りが `list[str]` → `list[PlanningTarget]`。呼び出しは runtime 1 箇所のみ (内部 API・外部契約でない)。テストの直接呼び出しがあれば追従。
- **`[ORCH]` registry 登録**は既存 `[ORCH]` ログ (shadow trigger 等) も activity.log に載せる副作用がある。これは可視性向上として許容 (元々 finance.log には出ていた)。

---

## 7. 実装順 (概略・詳細は plan で)

1. logging_setup に `[ORCH]` 登録 + テスト (A-1)
2. news TTL キャッシュ ラッパ (single-flight + negative cache) + config 2 値 + bootstrap 配線 + テスト (B, B-2)
3. `PipelineResult` に reason/derived_rr 追加 + 全 return 経路で設定 + reason 正規化 (改行除去/truncate) + テスト (A-3 データ契約)
4. detector `pairs_to_plan` → `PlanningTarget` 変更 + 呼び出し側追従 + テスト (A-5)
5. planning start / result ログ (既存 `_notify_planning_result` 位置に相乗り) + phase1 直接ログ + テスト (A-2, A-3)
6. フェーズ遷移 DEBUG 整理 (A-4)

順序根拠: 3・4 (データ契約 + trigger 受け渡し) を 5 (ログ本体) より前に確定させる — ログの正本が揃ってから出力を書く。

各ステップ TDD (RED → GREEN)。finance の uv/pytest は WSL 内実行厳守 ([[finance_uv_wsl_only]])。
