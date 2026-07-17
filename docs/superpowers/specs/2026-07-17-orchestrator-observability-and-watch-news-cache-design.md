# orchestrator 観測性 + watch news キャッシュ — 設計

**Date:** 2026-07-17
**Status:** 設計確定 (実装前)
**関連:** `2026-06-20-orchestrator-agent-loop-design-v2.md` (設計正本) / consolidated-roadmap §4 (paper 観察項目)
**関連 memory:** [[finance_main_logger_name]] / [[finance_uv_wsl_only]]

---

## 0. 背景 — 稼働ログ調査で判明した 3 問題

paper 稼働中 (Fiosracht, branch `feat/plan-quality-rr`) の `logs/finance.log` / `logs/activity.log` を実測して 3 つの観測性の穴を特定した。3 件とも既存挙動としては執行安全性に影響しないが、運用時の可視性を大きく損なう。**本 spec の修正 (特に §3 の news キャッシュ) は、実装を誤ると live trigger 判断を変えうる** — 失敗時セマンティクス (stale-if-error, §3.2/§3.7) を正しく守ることが挙動不変の条件になる。

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

1. `PipelineResult` に **`reason: str`** (全 outcome で必ず設定・ログ用の正本) と **`derived_rr: float | None`** を持たせる。**`derived_rr` は RR を導出済みの経路でのみ値を持ち、導出前 reject では None** (Low 指摘): scale-in evidence 不足 reject (planning_pipeline.py:235) は `derive_rr` 呼び出し前に return するため None。risk reject / plan_create は導出済みなので値あり。ログは `rr=<値>` を出すのは derived_rr が None でないときだけ。
2. **全 return 経路** (failed / direct_hold ×2 / reject ×N / plan_create) で `reason` を明示設定する。既存の `reasoning_summary` と揃える (direct_hold は「no opportunity」「position/current_plan unavailable」等、reject は planner/risk それぞれの理由)。
3. `reason` は**改行除去 + 長さ制限** (例: 200 字で truncate)。ログ 1 行を壊さない。
4. **phase1 observe** (pipeline 未注入) は `PipelineResult` を経由しないため、runtime.py:264 の分岐で直接 `[ORCH] planning result: ... decision=direct_hold reason=phase1 observe` を出す。
5. plan_create は既存の可視ログ (`📋 plan created`) と二重に出さない。既存ログを result 契約に寄せるか、既存を残して result 行だけ追加するかは実装時に統一 (どちらか一方)。
6. ログ出力は既存の `_notify_planning_result(pair, result)` (runtime.py:263) と同じ位置で行い、通知とログの正本を一元化する (再 query しない)。

**A-3b. ライフサイクル契約 — start 1 件に terminal result 1 件 (Medium 指摘):** `PipelineResult.reason` を全 outcome で定義したが、それだけでは result ログが全経路で保証されない:

- **pipeline 到達前の例外** (quote provider 失敗 / context build 失敗 / snapshot 保存失敗) は `PipelineResult` が存在しない (runtime.py:223 の try 内)。現状 except (runtime.py:276) は `finish_run(failed)` するだけでログを出さない。
- **pipeline が `outcome="failed"` を返した経路** (runtime.py:248) は `_notify_planning_result` を呼ばない (else 側 runtime.py:259 でのみ呼ぶ)。

→ **契約: `[ORCH] planning start` を 1 件出したら、必ず対応する `[ORCH] planning result` を 1 件出す** (成功 hold/reject/create・pipeline failed・pipeline 到達前 error のすべて)。実装:

- `run_planning_cycle` の各 pair 処理を、result ログを必ず 1 回出す構造にする (finally か、正常/failed/except の 3 経路すべてで出す)。
- pipeline 到達前 error は `[ORCH] planning result: pair=X decision=error reason=<例外要約>` を出す。
- pipeline failed は `decision=failed reason=<PipelineResult.error>`。
- reason は A-3 と同じ改行除去 + 長さ制限を通す。

テスト対象: quote provider 失敗 / context builder 失敗 / pipeline failed / 正常 hold・reject・create の各ケースで、start 1 件に result がちょうど 1 件対応すること。

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
- **ライフサイクル (A-3b)**: quote provider 失敗 / context builder 失敗 / pipeline failed / 正常 hold・reject・create の各ケースで、start 1 件に result がちょうど 1 件対応することを検証。
- `PipelineResult.reason` が全 return 経路で non-None・改行なし・長さ制限内であることを検証。
- `PipelineResult.derived_rr` が plan_create/risk reject で値を持ち、scale-in evidence 不足 reject では None であることを検証。
- `pairs_to_plan` が `PlanningTarget(pair, triggers)` を返し、複数 material 該当時に全 trigger を含むことを検証 (短絡しない)。
- trigger 理由生成で news 集計が余分に走らないことを検証 (aggregate 呼び出し回数)。
- `[ORCH]` が `_ACTIVITY_PREFIXES` に含まれることを検証 (registry 登録の回帰防止)。

---

## 3. コンポーネント B — watch news の short-TTL キャッシュ

### 3.1 目的

watch loop (1sec) が保持中 plan ごとに毎 tick news をフル集計するのを止める。news は RSS 30 分更新なので、短い TTL キャッシュで十分。計算 (vector store 検索 + 集計) とログの両方を根絶する。

### 3.2 設計

`make_news_provider` (context_builder.py:399) が返す `NewsProvider` を **TTL キャッシュでラップ**する。

**並行実行と失敗時の設計 (Medium/High 指摘対応):** planning loop と watch loop は**別スレッド**で同時に動き、同じ context builder / news_provider を共有する (runtime.py:1297 でスレッド起動)。単純 dict キャッシュだと (a) TTL 境界で両スレッドが同時 miss して二重集計する、(b) inner が例外だとキャッシュされず `_build_news` (context_builder.py:269) が毎秒例外を記録して再試行する — `[ORCH]` を activity 対象にするため障害時に新たなログ洪水になる。→ **pair 単位 lock (single-flight) + stale-if-error** を仕様に含める。

**失敗時セマンティクスの制約 (High 指摘):** 失敗時に `_empty_news()` (sentiment=None) を「成功」として返してはいけない。`_build_news` (context_builder.py:274) がそれを取得成功とみなし `_ref.as_of = now` を付け、`_news_conflicts` (runtime.py:776) は sentiment=None で conflict=False になる。**結果、直前まで反対方向の強い news があっても refresh 失敗後の TTL 間だけ news_conflict による失効が無効化され、entry 成立時に trigger へ進んでしまう** — live trigger 判断に影響する (「執行安全性に影響しない」前提の違反)。したがって:

- **failure TTL で再集計を止める (High 指摘)**: `inner(pair)` を**呼ぶ前**に直近失敗時刻を判定し、negative TTL 内なら inner を呼ばず stale/unavailable を返す。→ 計算 (RAG 集計) とログの**両方**を止める。失敗記録はログ頻度制御だけでなく再集計抑止に使う。
- **stale-if-error**: 過去の成功値があれば、失敗中はその**古い成功値をそのまま返す** (as_of は実際の成功取得時刻を維持)。→ 直前の逆行 news は保持され、news_conflict は生き続ける。
- **成功値が一度も無い場合**: `status="unavailable"` を明示した news ブロックを返す。→ `_build_news` / watch 側が「取得できていない」と識別できる。
- **成功で failure 状態を解除**する。

**provider 返却型 (3 状態):** cached provider は §7 news dict に `status` と `as_of` を必ず付けて返す:

- `status="ok"`: 新規取得成功。`as_of` = 取得時刻。
- `status="stale"`: refresh 失敗で前回成功値を返却。`as_of` = **前回成功時刻** (変更しない)。sentiment/confidence/top_reasons は前回成功値。
- `status="unavailable"`: 成功履歴なし。sentiment=None / confidence=None / top_reasons=[] / `as_of=None`。

```python
def make_cached_news_provider(
    inner: NewsProvider, *, ttl_seconds: float,
    negative_ttl_seconds: float, clock: Callable[[], datetime],
) -> NewsProvider:
    """pair 単位で成功値 (news dict, 成功時刻) を保持。TTL 内は再集計せず返す。
    pair 単位 lock で single-flight。failure TTL 内は inner を呼ばず
    stale (前回成功値) / 成功履歴なしは unavailable。返却は status+as_of 付き。"""
    cache: dict[str, tuple[dict, datetime]] = {}   # pair -> (成功 news, 成功時刻)
    failures: dict[str, datetime] = {}             # pair -> 直近失敗時刻 (再集計 + ログ抑止)
    locks: dict[str, Lock] = defaultdict(Lock)
    guard = Lock()

    def _ok(value: dict, at: datetime) -> dict:
        return {**value, "status": "ok", "as_of": at.isoformat()}

    def _stale(value: dict, at: datetime) -> dict:
        return {**value, "status": "stale", "as_of": at.isoformat()}

    def _unavailable() -> dict:
        return {"sentiment_score": None, "confidence": None,
                "top_reasons": [], "status": "unavailable", "as_of": None}

    def provider(pair: str) -> dict:
        now = clock()
        hit = cache.get(pair)
        if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
            return _ok(hit[0], hit[1])
        with guard:
            lock = locks[pair]
        with lock:                                 # single-flight
            hit = cache.get(pair)
            if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
                return _ok(hit[0], hit[1])
            # failure TTL 内: inner を呼ばない (計算もログも止める)。
            failed_at = failures.get(pair)
            if failed_at is not None and (now - failed_at).total_seconds() < negative_ttl_seconds:
                return _stale(hit[0], hit[1]) if hit is not None else _unavailable()
            try:
                value = inner(pair)                # ← ここでのみ aggregate_news_sentiment が走る
            except Exception as exc:
                failures[pair] = now               # 再集計抑止の起点 + 1 回だけログ
                logger.warning("[ORCH] news aggregate failed for %s: %s", pair, exc)
                return _stale(hit[0], hit[1]) if hit is not None else _unavailable()
            cache[pair] = (value, now)
            failures.pop(pair, None)               # 成功で failure 解除
            return _ok(value, now)
    return provider
```

- **キャッシュキー = pair**。cache 保存値 = §7 news ブロック dict (`sentiment_score` / `confidence` / `top_reasons`)。status/as_of は返却時に付与 (cache には生値のみ保存)。
- **成功 TTL 既定 = 60s** / **negative(失敗) TTL 既定 = 30s** (config 化: `OrchestratorConfig.entry.news_cache_ttl_seconds: float = 60.0` / `news_cache_negative_ttl_seconds: float = 30.0`。`__post_init__` で有限 > 0 を検証)。
- **clock は注入** (db_now を渡す)。テストで時間を進められるよう関数引数にする。Date.now 直呼びしない。
- ラップ位置は bootstrap で `make_news_provider` の戻りを `make_cached_news_provider` で包む 1 箇所。`DecisionContextBuilder` に渡る provider がキャッシュ済みになる。

### 3.6 `_build_news` の stale/unavailable 対応 (High 指摘)

現状 `_build_news` (context_builder.py:256) は「provider 成功 → `_ref.as_of = now`」「provider 例外 → 空 news + `_ref=None`」の 2 分岐。cached provider は例外を投げず status/as_of 付き dict を返すため、それを使って分岐させる:

- `_build_news` は provider 応答の **`as_of` をそのまま `_ref.as_of` に使う** (今の now で上書きしない)。→ stale の場合 trace 上「前回いつ取得した news か」が正しく残る。
- `_ref` に provider の **`status` (ok/stale/unavailable)** を残す。
- `status="unavailable"` の news は sentiment=None のまま通す (fail-open, §3.7)。

### 3.7 watch の news_conflict と unavailable の扱い

- **`status="stale"`** (直近成功値) が返る場合: `_news_conflicts` は従来通り生の sentiment_score で判定する。→ 直前の逆行 news は保持され失効が効く (High 指摘の中核)。
- **`status="unavailable"`** (成功値なし) の場合: sentiment=None なので `_news_conflicts` は False。これは「news で失効させる根拠が無い」= 従来の空 news と同じ挙動で、**block しない** (fail-open)。**technical とは異なる**: watch evaluator は technical が `status != "ok"` なら trigger を **block** する (fail-close, watch_evaluator.py:138)。news は補助材料なので**意図的に fail-open** とし、news 取得不能を理由に active plan を止めない。この非対称を spec 上で明示的に決定とする ([[finance_risk_management_philosophy]] — 補助材料の取得不能で執行を全停止するのは過剰)。

### 3.3 build 経路との関係

- `build()` (planning, snapshot materialize) も同じ provider を使う。planning は 60s cadence なので TTL 60s ならほぼ毎回 miss して新鮮な値を取る (planning material 判定の精度は維持)。planning cycle 由来の二重集計 (7/17 ログの EUR/USD 60s 2 行) は build 経路でなく material landing 経路 (§3.4) 由来なので、そちらのキャッシュで解消する。
- watch (1sec) は TTL 内で hit し続け、60s に 1 回だけ実集計。→ [AGGREGATE] ログは pair あたり最大 60s に 1 回に減る。
- material landing 経路 (`make_news_material_provider`, landing_providers.py) は planning loop の material 判定で使われ、`make_news_provider` とは別の provider を返す (戻り値が dict でなく NewsSentiment)。§3.4 で別途キャッシュする。

### 3.4 B-2: material landing 経路の二重集計

`make_news_material_provider` の `get_news_impact` + `get_news_key` が 1 回の material 判定で各々 `_aggregate` を呼び、同 pair を 2 回集計する (impact≥閾値時)。加えて `commit_seen` でも `get_news_key` を呼ぶため 1 判定で最大 3 回集計しうる。

- **既存の例外握り潰しを迂回しない (Medium 指摘):** `make_news_material_provider` の `_aggregate()` は現状 `except Exception: return None` で例外を握り潰す (landing_providers.py:112)。この関数をそのまま共通キャッシュで包むと **`None` を成功値としてキャッシュ**し、stale 値が維持されない (stale-if-error が迂回される)。→ **`_aggregate()` の内部 try/except を削除し、例外をキャッシュ層に伝搬させる**。キャッシュ層の共通ヘルパが例外を捕捉して stale/unavailable を判定する (§3.2 と同一セマンティクス)。
- **対応:** `_aggregate` (NewsSentiment 返し) 用に §3.2 と**同じ失敗セマンティクス** (成功 TTL hit / failure TTL 内は再集計せず stale / 成功履歴なしは None) を持つ内部ヘルパを共有する。キャッシュ実体は関数ごとに別。共有するのは「pair→(value, 成功時刻) を TTL + failure TTL で判定するロジック」。
- material 経路の失敗時返却: stale があれば直近成功 NewsSentiment、無ければ None (get_news_impact→0.0 / get_news_key→None、既存の None 契約を維持)。
- TTL・clock は §3.2 と同じ `news_cache_ttl_seconds` / `news_cache_negative_ttl_seconds` / db_now を使う。
- これで planning material 判定由来の [AGGREGATE] (60s 間隔・EUR/USD 2〜3 行) も 1 行に減る。
- **キャッシュ実体を 2 つ持つことの整合:** watch 経路 (dict provider) と material 経路 (NewsSentiment) は別プロセス位置・別頻度で呼ばれるため、キャッシュを共有せず各々が独立に最大 TTL 秒だけ古い値を返す。値のズレは最大 TTL 秒で、判断品質に影響しない (news は 30 分粒度)。

### 3.5 テスト

- 同一 pair を TTL 内で複数回呼ぶと inner (aggregate) が 1 回しか呼ばれないことを mock で検証。
- TTL 超過後は再集計されることを clock を進めて検証。
- material provider の impact + key (+commit_seen) 連続呼び出しで aggregate が 1 回に集約されることを検証。
- **並行テスト**: 2 スレッドが同時に同 pair を miss しても inner が 1 回しか呼ばれない (single-flight)。
- **failure TTL 再集計抑止テスト (High)**: inner が失敗した後、negative TTL 内の再呼び出しで **inner が呼ばれない** (呼び出し回数 = negative TTL 内で 1 回)。ログ回数だけでなく **inner 呼び出し回数**を検証する。
- **stale-if-error テスト (High)**: 成功して sentiment を得た後、inner が例外を投げても直近成功値 (`status="stale"`) が返る・as_of が成功時刻を維持する。→ **「成功後の refresh 失敗でも news_conflict が消えない」**: 逆行 news で成功 → refresh 失敗 → `_news_conflicts` が引き続き True を返すことを検証。
- **unavailable テスト**: 一度も成功していない状態で inner 例外 → `status="unavailable"` news が返る・sentiment=None。
- **成功で failure 解除テスト**: 失敗 → 成功 → 失敗 で、2 回目失敗が再び inner を呼ぶ (failure 状態が成功でクリアされる)。
- **material stale テスト (Medium)**: material provider も「成功後の refresh 失敗で前回 sentiment (impact/key) が維持される」ことを検証。`_aggregate` 例外がキャッシュ層に伝搬し stale が返ることを確認。

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

- **挙動不変性:** news キャッシュは成功値を最大 TTL 秒古くするだけ。watch の news_conflict 判定は 60s 粒度で更新 (現状の毎秒判定は過剰)。planning material 判定は 60s cadence と同期するので実質不変。**失敗時は stale-if-error で直近成功値を維持する** (空 news に化かさない) ため、refresh 失敗でも news_conflict による失効は生き続ける — live trigger 判断を変えない (High 指摘の要件)。一度も成功していない場合のみ sentiment=None (unavailable) となり、これは従来の空 news と同じ fail-open 挙動 (§3.7 で明示決定)。
- **ログ増減:** activity.log に planning start/result が増える (60s ごと数行) が、watch 由来 [AGGREGATE] が pair あたり毎秒 → 60s に 1 回へ激減。差引で activity.log は読みやすくなる。
- **config 追加:** `news_cache_ttl_seconds` (既定 60s) / `news_cache_negative_ttl_seconds` (既定 30s)。未設定時は既定で挙動する (後方互換)。
- **detector 戻り値変更:** `pairs_to_plan` の戻りが `list[str]` → `list[PlanningTarget]`。呼び出しは runtime 1 箇所のみ (内部 API・外部契約でない)。テストの直接呼び出しがあれば追従。
- **`[ORCH]` registry 登録**は既存 `[ORCH]` ログ (shadow trigger 等) も activity.log に載せる副作用がある。これは可視性向上として許容 (元々 finance.log には出ていた)。

---

## 7. 実装順 (概略・詳細は plan で)

1. logging_setup に `[ORCH]` 登録 + テスト (A-1)
2. news TTL キャッシュ ラッパ (single-flight + stale-if-error + unavailable + ログ抑制) + config 2 値 + テスト (B §3.2)
3. `_build_news` を as_of/status ベースに変更 (stale as_of 維持・unavailable 標識) + テスト (§3.6/§3.7) + bootstrap 配線
4. material landing 経路のキャッシュ (B-2 §3.4) + テスト
5. `PipelineResult` に reason/derived_rr 追加 + 全 return 経路で設定 + reason 正規化 + テスト (A-3 データ契約)
6. detector `pairs_to_plan` → `PlanningTarget` 変更 + 呼び出し側追従 + テスト (A-5)
7. planning start / result ログ + ライフサイクル契約 (start:result = 1:1) + phase1・failed・到達前error 全経路 + テスト (A-2, A-3, A-3b)
8. フェーズ遷移 DEBUG 整理 (A-4)

順序根拠: 2・3 (キャッシュ失敗セマンティクス) を最優先で正しくする — live trigger 判断に影響する唯一の箇所 (High)。5・6 (データ契約 + trigger 受け渡し) を 7 (ログ本体) より前に確定させ、ログの正本が揃ってから出力を書く。

各ステップ TDD (RED → GREEN)。finance の uv/pytest は WSL 内実行厳守 ([[finance_uv_wsl_only]])。
