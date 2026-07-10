# Discord 承認ゲート (approval gate) 設計 spec

日付: 2026-07-05 (**2026-07-09 codex spec レビュー 1巡目 7件 + 2巡目 5件反映** — F-1/F-2/F-4/F-5/F-6/F-7 改訂)
状態: 設計確定 (実装前)
関連: `2026-07-04-consolidated-roadmap.md` §1-2 (承認ゲート採用推奨・却下=ラベルデータ),
`2026-07-05-planner-position-plan-context.md` (独立 spec、依存なし・並行可)

対象リポジトリ: finance (API/lifecycle/watch) + discord_bot (cog/UI)。
権威は常に finance 側。discord_bot は UI アダプタに徹する。

---

## 1. 目的

- PlannerAgent の plan を人間が承認してから発注待機 (arm) させる
- 承認/却下/放置の3値ラベル + 反実仮想の結末で「人間ゲートの付加価値」を計測し、
  自動化卒業 (ゲート撤去) の根拠データを作る
- paper 段階で導入し、live 昇格後もそのまま使う

## 2. 確定済み設計判断 (2026-07-05 ユーザー合意)

| # | 論点 | 決定 |
|---|---|---|
| G-1 | 通信方式 | REST + bot 側 polling (新規インフラなし)。将来必要なら SSE 追加で購読化、approve/reject の REST は不変 |
| G-2 | 返答待ち中の TTL 切れ | 既存 sweep で expired に落とし、bot がメッセージを「期限切れ (未回答)」に edit |
| G-3 | 「観察のみ」の第3ボタン | 設けない。放置 = 観察 (TTL で expired、反実仮想は却下と同じ仕組みで追跡) |
| G-4 | チャンネル | 単独チャンネル1本 |
| G-5 | 却下理由 | ❌ボタン → modal (自由記述・空送信可)。分類語彙は溜まってから |
| G-6 | 承認の単位 | plan 単位。新 plan に置換されたら再び pending_approval (承認は引き継がない) |
| G-7 | 却下/放置 plan の追跡 | watch が entry 成立を記録し続け、hindsight が反実仮想 pnl_r (spread 込み) を採点。検証対象は plan でなく**却下という判断そのもの** |

## 3. finance 側の構造変更

### F-1: plan status の追加

`PLAN_STATUSES` に `pending_approval` / `rejected` を追加。lifecycle:

```
pipeline 作成 (gate ON では publish が pending_approval — F-6)
   └→ pending_approval ──承認──────→ active ──成立──→ triggered (以降現行どおり)
            ├──却下──────→ rejected    (terminal・status はもう動かない。反実仮想追跡は F-4)
            ├──放置 (TTL)─→ expired     (watch の expiry 判定 (※)。gate_decision='unanswered' を刻印)
            ├──invalidation→ invalidated (watch の invalidation 判定 — active と同じ扱い。
            │                構造的に死んだ plan を承認可能なまま放置しない。
            │                **こちらも gate_decision='unanswered'** — 人間の判断なしに
            │                終端した点は TTL 放置と同じ (G-3 の拡張解釈。内訳は status で分離))
            └──新 plan 置換→ superseded  (F-3。システム都合の取り下げ = gate_decision NULL のまま)
```

(※) 「既存 sweep」の実体は watch loop の expiry 判定 (`_evaluate_plan` 優先順 1、
`runtime.py`)。独立した janitor は無いので、pending_approval を watch 対象に加える
こと (F-4) が expiry / invalidation 処理の前提になる。

- position-context spec (P-2) の `current_plan` ブロックの対象を
  {active} → {active, pending_approval} に拡張する (返答待ち plan も planner から
  見えるようにする。status 追加とセットで本 spec 側の作業)
- **approve / reject の遷移は専用 helper `try_decide_gate` で行う** (F-2b)。
  既存 `try_claim_plan_status` は status + updated_at しか更新できず、
  gate_decision / gate_decided_at / gate_reason を同一 UPDATE で残せないため
  (codex Medium)。排他の考え方は同じ: `WHERE status='pending_approval'` の
  条件付き単一 UPDATE、rowcount で勝敗判定。二重クリック・承認と却下の競合は
  自然解決、負けた側は 409
- gate OFF (既定) は現行どおり active で publish — **挙動不変** (新列はすべて NULL)

### F-2: trade_plans への nullable 列追加 (idempotent ALTER)

| 列 | 内容 |
|---|---|
| `gate_decision` | **永続ラベル正本**: `approved` / `rejected` / `unanswered`。決定時 (approve/reject) または pending が人間の判断なしに終端した時 (**expired / invalidated の両方**) に一度だけ刻印し、以後不変。NULL = gate OFF plan・未決定・superseded (システム都合の取り下げ) |
| `gate_decided_at` | 承認/却下の時刻 (放置は NULL) |
| `gate_reason` | 却下理由 自由記述 (任意) |
| `gate_message_id` | Discord メッセージ ID (bot 再起動後の突合用。保存経路は F-5 の gate_message endpoint) |
| `cf_state` | 反実仮想追跡の状態機械 (F-4): NULL=追跡中(または対象外) / `would_trigger`=pending 中に entry 初成立 (stamp 済・cf 行は未記録) / `triggered`=cf shadow_trigger 行を記録済 / `invalidated`=追跡窓を構造的に閉鎖 (latch) / `superseded`=pending が新 plan に置換され窓終了 (F-3。件数集計用マーカー — gate_decision は NULL のままなので、これが無いと gate OFF の superseded と区別できない) |
| `cf_stamped_at` / `cf_stamp_price` / `cf_stamp_spread_pips` | stamp (pending 中の entry 初成立時点の時刻・mid・spread)。cf 行を後から記録する際の triggered_at / trigger_price / spread_pips になる (hindsight の spread 込み採点に必須) |

**ラベルを status から復元する方式は廃止** (codex Medium): approved plan は後続で
triggered / expired / invalidated / superseded 等へ遷移し、gate OFF の通常 plan と
区別できなくなる。`gate_decision` が唯一の正本。新テーブルは作らない。

**shadow_triggers 側のスキーマ変更は不要**: plan_id UNIQUE 制約 (uq_shadow_triggers_plan_id)
により trigger 行は plan 1件につき生涯 1 本 (real か counterfactual のどちらか — F-4)。
行の種別は `trade_plans.gate_decision` との JOIN で引ける (rejected / unanswered の plan の
行 = counterfactual、それ以外 = real)。旧 spec の「記録時点の plan status を残す」列は不要。

### F-2b: gate 決定の専用 helper (store)

```python
def try_decide_gate(plan_id, decision: Literal["approved", "rejected"],
                    *, reason: str | None = None) -> bool:
    # UPDATE trade_plans
    #    SET status = ('active' if approved else 'rejected'),
    #        gate_decision = decision, gate_decided_at = db_now(),
    #        gate_reason = reason, updated_at = db_now()
    #  WHERE plan_id = ? AND status = 'pending_approval'
    # → rowcount == 1 で勝ち。単一文なので TOCTOU なし (try_claim_plan_status と同思想)
```

status 遷移とラベル・時刻・理由を**同一 UPDATE** で原子的に残す (codex Medium)。
API 層は False → 409 に写像。unanswered の刻印は watch の pending→expired /
pending→invalidated 遷移側で行う (同様に `WHERE status='pending_approval'` の
条件付き UPDATE で status + gate_decision='unanswered' を一括更新 — approve との
race も rowcount 排他で解決)。

**cf finalize の原子 helper (codex 2巡目 High / 3巡目 High×2 反映):** cf 行化はすべて
`finalize_cf_trigger(plan_id, ...)` として store に置き、**単一 transaction** で
(1) `UPDATE cf_state → 'triggered'` の rowcount claim
(2) `plan_cf_trigger` decision INSERT (3) shadow_triggers INSERT
(4) hindsight enqueue INSERT を行う (既存 `record_shadow_trigger` /
`record_hindsight_evaluation` / `record_decision` は個別 commit なので流用しない —
中間クラッシュで「cf_state だけ進んで行がない」等の孤児が出る。decision も tx 内に
含めることで claim 負け・再試行時に偽 decision が残らない)。

**claim の許可条件 (3巡目 High#1 — 承認済み plan の保護):**
- from `'would_trigger'` (stamp 済み終端): `gate_decision IN ('rejected','unanswered')` 必須
- from `NULL` (stamp なしの直接記録): `status='rejected' AND gate_decision='rejected'` のみ

承認済み plan (gate_decision='approved') は stamp が残ったまま real trigger →
**live 発注失敗で invalidated** (runtime の is_executed=False 経路) になり得る。
gate_decision 条件が無いと「terminal + would_trigger」に一致して誤 cf 復旧され、
偽 decision + real 行との UNIQUE 衝突を起こす。gate_decision で構造的に排除する。

hindsight 行は real と同内容の pending 行 (evaluator 未注入時は real 同様 enqueue
しない)。rowcount 0 なら何もしない (冪等・再試行安全)。**UNIQUE(plan_id) 違反時は
状態を進めず rollback し整合性エラーとしてログ** (無条件で cf_state を進めると
「state だけ進んだ孤児」を再発させる — 3巡目 High#2)。status 遷移 (try_decide_gate /
expiry / invalidation) と finalize の**間**のクラッシュは F-4 の finalize 待ち集合が
次 tick で回収する。

### F-3: supersede の対象拡張

`supersede_active_plans` の対象を status ∈ {active, **pending_approval**} に拡張。
新 plan 作成時に返答待ち plan も置換される (G-6)。置換された pending plan は
superseded であり rejected ではない (人間の判断なしラベル = gate_decision NULL)。
superseded になった pending plan は反実仮想追跡の対象にもしない (F-4 の watch 対象から
status 遷移で自然に外れる。システム自身が取り下げた plan の「承認していたら」は
測定対象の判断が存在しないため。stamp データが残っていても cf 行は起こさない)。
置換時、旧 status が pending_approval だった plan には **`cf_state='superseded'` を刻印**
する (F-7 の superseded pending 件数の集計マーカー。stamp 済みなら would_trigger を
上書きしてよい — 窓は閉じた)。
rejected は置換対象外 (terminal のまま・追跡窓は expires_at まで継続)。

### F-4: watch の反実仮想追跡 (本体・要厚めレビュー)

**前提となる制約 (2026-07-09 判明):** `shadow_triggers` は `UNIQUE(plan_id)`
(uq_shadow_triggers_plan_id、real trigger 二重記録の defense-in-depth)。pending 中に
反実仮想行を書くと、その plan が後に承認→active→**本物の trigger** に至った時に
UNIQUE 違反で本番記録が壊れる。制約の緩和 (partial index 化) は SQLite では table
rebuild になるため採らない。→ **「stamp → 終端時に記録」方式**: cf 行は「もう real
trigger が絶対に起こり得ない plan (rejected / unanswered 終端 = expired・invalidated)」
にのみ記録する。これにより real 行と cf 行は構造的に排反 (plan 1件 = trigger 行は生涯 1 本)。

**設計原則: 評価意味論は active と同一、違うのは action 境界のみ。**
非 active plan も `_evaluate_plan` と同じ順序 (invalidation/expiry → entry →
freshness final wall) で評価する。承認済みとの成績比較が「同じ物差し」であるための
要件。ただし action は分岐する:

| status | expiry 成立 | invalidation 成立 | entry 初成立 (freshness OK) |
|---|---|---|---|
| active (現行) | status=expired | status=invalidated | claim(active→triggered) + shadow 記録 + (live なら執行) |
| pending_approval | status=expired + gate_decision='unanswered' 刻印。**stamp 済なら cf finalize** | status=invalidated + **gate_decision='unanswered' 刻印** (real 遷移 — 死んだ plan を承認可能なまま放置しない。bot が message edit)。**stamp 済なら cf finalize** (「承認待ち中に entry できたのに判断前に死んだ」= gate 遅延の重要サンプル、捨てない — codex 2巡目 Medium) | **stamp のみ** (cf_state='would_trigger' + cf_stamped_at/price/spread_pips)。shadow 行は書かない。plan は pending のまま (承認可能) |
| rejected | 追跡窓終了 (status は不変。watch 対象クエリの expires_at 条件で自然に外れる) | **cf_state='invalidated' で latch** (status は不変)。以後評価しない | **finalize_cf_trigger で cf 行を即記録** (from=NULL の claim。terminal なので real と衝突しない) |

- **rejected の invalidation latch が必須な理由**: real 経路は invalidation で status が
  遷移する (= latch) が、rejected は status が動かない。latch なしだと
  「invalidation 成立 → 価格が戻る → entry 成立」の順で、承認世界なら invalidation で
  死んでいたはずの plan に cf 行が付く (誤った反実仮想)。cf_state がその latch。
- **reject 時の cf 化**: `try_decide_gate(rejected)` 成功後、stamp (cf_state='would_trigger')
  があれば `finalize_cf_trigger` (F-2b の原子 helper) で cf 行を起こす
  (triggered_at=cf_stamped_at、trigger_price=cf_stamp_price、spread_pips=cf_stamp_spread_pips)。
  エントリー点は「最初に条件成立した瞬間」で正確。
  stamp がなければ rejected のまま watch 継続 (expires_at まで)。
- **cf 行の記録は専用経路** (`_record_shadow_trigger` の拡張ではない — codex High):
  status claim しない / `_execute_live_trigger` に到達する経路がない / order_intent を
  作らない / broker 参照を持たない。decision は `plan_cf_trigger` type で記録し、
  real の `plan_trigger` と集計上も分離する。risk_gate_result は NULL、snapshot_id は
  NULL 可 (hindsight は trigger 行の price/sl/tp/spread + OHLCV だけで採点できる)。
  hindsight は finalize tx 内で real と同内容の pending 行を enqueue する (F-2b。
  過去時刻の triggered_at でも elapsed 判定は正しく動く)。
- **dedupe / 排他**: stamp と cf_state 遷移は plan 行への条件付き UPDATE
  (`WHERE cf_state IS NULL` 等) の rowcount claim で行う。UNIQUE(plan_id) は最終
  防衛線としてそのまま残す。
- **執行境界は不変**: live 執行と triggered claim は active のみ。rejected / pending が
  誤執行される経路は構造的に存在しない (cf 経路は執行コードを含まない)。
- **watch 対象クエリ**: `status='active'` (現行) ∪ `status='pending_approval'` (全件 —
  expiry/invalidation 遷移の責務があるため stamp 後も対象) ∪
  `status='rejected' AND expires_at > now AND cf_state IS NULL` (cf 解決済み・窓閉鎖済みは
  恒久的に外れる — rejected の無限蓄積で watch が肥大しない) ∪
  **finalize 待ち集合 (crash recovery — codex 2巡目 High)**:
  `status IN ('rejected','expired','invalidated') AND cf_state='would_trigger'
  AND gate_decision IN ('rejected','unanswered')`。gate_decision 条件は必須 —
  承認済み plan (approved) が real trigger 後に発注失敗で invalidated になった場合を
  除外する (3巡目 High#1)。status 遷移 tx と finalize tx の間でクラッシュした plan が
  ここに残る。watch tick が見つけ次第 `finalize_cf_trigger` を再実行 (claim ベースで
  冪等)。成功すると cf_state='triggered' になり集合から恒久的に抜ける —
  復旧不能な取りこぼしを作らない。
- **stamp 済みで承認された plan**: stamp は残るが cf 行にはならない (real trigger が
  正本)。cf_stamped_at と実 trigger 時刻の差 = **承認遅延コストの測定素材** (副産物、
  F-7 の headline 3 ラベルには含めない)。

### F-5: API 5本 (既存 FastAPI 8811 / X-API-Key)

| endpoint | 動作 |
|---|---|
| `GET /orchestrator/plans?status=pending_approval` | pending 一覧 (plan_id, pair, direction, entry_summary, SL/TP, expires_at, created_at, reasoning 要約, **gate_message_id** — bot 再起動時の投稿済み判定用) |
| `GET /orchestrator/plans?posted_within_hours=N` | **再起動 reconcile 用** (codex 2巡目 Low-Med): `gate_message_id IS NOT NULL AND updated_at >= now-N h` の plan を status 不問で返す (status / gate_decision 込み)。bot 停止中に pending から消えた投稿済み plan のメッセージ edit 復旧に使う |
| `GET /orchestrator/plans/{id}` | plan 詳細 (status + gate_decision + gate_decided_at + gate_reason)。polling で pending から消えた plan の結末判定 (bot の message edit 用) はこれで行う |
| `POST /orchestrator/plans/{id}/approve` | `try_decide_gate(approved)` 成功→200 {status: active}、失敗→409 (決定済み/期限切れ) |
| `POST /orchestrator/plans/{id}/reject` body `{reason?: str}` | 同上 (rejected)。reason は gate_reason へ |
| `POST /orchestrator/plans/{id}/gate_message` body `{message_id: str}` | bot が Discord 投稿直後に呼ぶ (codex Medium: 保存経路がないと再起動突合が成立しない)。冪等 (同値上書き可)。404=plan なし |

**API → OrchestratorStore の経路 (codex 2巡目 Medium / 3巡目 Low-Med で文言確定):**
現状 `APIState` (`src/api/_state.py`) に orchestrator store は無く、`start_api_server()`
でも注入されていない。既存パターン踏襲で **`APIState.orchestrator_store` フィールドを
追加し、`start_api_server()` の引数に加えて main から注入する**。注入するのは main で
生成した OrchestratorStore で、runtime (bootstrap) 側とはインスタンスが別だが、
**engine は `_get_engine` の path 単位キャッシュで同一実体**を共有する。Store は
Session-per-call で engine 以外の状態を持たないため、インスタンス分離に挙動差はない
(「同一 DB・同一 engine を参照」が正確な要件。bootstrap への引き回しはしない)。
route 内で prices_db_path から都度開く案は採らない (毎 request の Store 生成は無駄・
注入パターンからの逸脱)。

**reasoning 要約の取得元 (codex Medium で未定義だった点):** trade_plans には
reasoning が無い。**最新の `plan_create` decision (orchestrator_decisions.plan_id で
引く) と JOIN して返す**。plan 側への冗長保存はしない (真実源を二重化しない)。
表示整形は `plan_view.plan_to_row` を共用 (CLI plans コマンドの「reasoning は F-5 時に
追加」注記どおり、ここで reasoning フィールドを足す)。

### F-6: config

`orchestrator.approval_gate: bool = False` (schema 既定 OFF = 挙動不変)。

**分岐点は「作成時 status」ではなく「最後の publish」** (codex High): 現行 pipeline
(`planning_pipeline._commit_plan`) は orphan 防止のため
`create_trade_plan(status="requires_replan")` → decision → vote → supersede →
最後に `update_plan_status('active')` の write 順序を持つ。途中クラッシュした plan が
active として可視化されない保証はこの順序に依存する。gate ON でもこの順序は不変とし、
**最後の publish の 1 箇所だけ** `'active'` / `'pending_approval'` に分岐する。
supersede (F-3 拡張版) は publish 前に走る点も現行どおり。

付随: PipelineResult / plan 作成通知・daily summary の status 集計が pending_approval
を含み得ることを確認する (文言・集計の追従は実装時に洗う)。

### F-7: metrics / daily summary

`get_shadow_metrics_raw` と daily summary に gate ラベル別の行を追加:
approved / rejected / unanswered の件数・trigger 率・hindsight 平均 pnl_r。
ラベルは `trade_plans.gate_decision` の GROUP BY (F-2 で正本化。status からの復元はしない)。

**real / counterfactual の分離規則 (集計汚染の防止):**

- trigger 行は plan 1件につき 1 本 (UNIQUE) で、cf 行は gate_decision ∈
  {rejected, unanswered} の plan にしか存在しない (F-4)。→ **既存の実性能集計
  (trigger 率・hindsight pnl_r・daily summary) は
  `(gate_decision IS NULL OR gate_decision NOT IN ('rejected','unanswered'))`
  の plan の行に限定する**。SQL の `NOT IN` は NULL 行を落とすため、
  **`IS NULL OR` を省くと gate OFF の通常 plan (NULL) が実性能集計から消える**
  (codex 2巡目 Medium — SQLAlchemy 実装時も同じ罠)。この除外を入れないと、
  gate 導入と同時に cf サンプルが実性能に混入する
- decision 集計も同様: real は `plan_trigger`、cf は `plan_cf_trigger` で type から分離
- **gate ラベル別集計にも trade_horizon を適用する** (3巡目 Medium): daily summary は
  運用中 horizon で絞って集計するため、gate 行だけ全期間値になると swing が混入する
- **unanswered の内訳は status で分離可能**: expired = TTL 満了まで無応答 /
  invalidated = 応答前に構造死 (応答機会が短かった可能性あり — 放置率の解釈時に
  区別する)。superseded pending (gate_decision NULL) は 3 ラベル外 (システム都合の
  取り下げ・判断が存在しない)。件数だけ別行で出す — 集計は `cf_state='superseded'`
  マーカー (F-3。gate_decision NULL だけでは gate OFF の superseded と区別できない)
- 副産物: stamp 済みで承認された plan の cf_stamped_at と実 trigger 時刻の差 =
  **承認遅延コスト** (即承認なら幾ら取れたか)。headline には含めず別行

人間ゲートの付加価値 = E[pnl_r|approved] − E[pnl_r|rejected 反実仮想]。
放置の反実仮想が大きく正なら UI/運用の問題 (判断品質でなく通知到達の問題) と
切り分ける。卒業判定は数値蓄積後にユーザーが行う (自動化しない)。

## 4. discord_bot 側 (cog 拡張)

- `FinanceCog` に `tasks.loop(seconds=10)` の polling を追加。`cog_load` で start /
  `cog_unload` で cancel、`before_loop` で `wait_until_ready`
- 新規 pending plan → 単独チャンネルに embed (pair/方向/entry 条件/SL/TP/TTL/理由要約)
  + ✅承認 / ❌却下 ボタン。❌ は modal (理由任意・空可)
- ボタンは **persistent view** (`timeout=None` + 固定 `custom_id` に plan_id を埋める)。
  bot 再起動を跨いでも TTL 8h の間ボタンが生きる
- クリック → 既存 `FinanceClient` で approve/reject POST → 応答でメッセージ edit
  (✅承認済み / ❌却下 / ⏰期限切れ / 🔄新 plan に置換)。409 は「既に決定済み」として edit
- bot 側状態は plan_id→message_id の揮発キャッシュのみ (dedupe 用)。正本は finance DB。
  投稿直後に `POST .../gate_message` で message_id を finance へ永続化 (F-5)。
  **起動時 reconcile (1回)**: `GET /orchestrator/plans?posted_within_hours=24` で
  投稿済み plan を status 不問で取得し、(1) pending なのに gate_message_id NULL →
  投稿 (2) 非 pending で投稿済み → 結末 (gate_decision/status) でメッセージ edit。
  bot 停止中に expired/superseded/決定済みへ遷移した投稿の取り残しを解消
  (codex 2巡目 Low-Med)。以後の通常 polling は pending 一覧のみ
- polling で pending から消えた plan は `GET /orchestrator/plans/{id}` (F-5) の
  status + gate_decision で結末を判定してメッセージ edit
- (任意) pending 一覧に cf_state を含め、stamp 済み (would_trigger) の plan は
  メッセージに「⚡条件成立済み」を追記 edit — 判断催促の UX。実装は後回しで可
- `FinanceClient` 追加メソッド: `pending_plans()` / `plan_detail(id)` /
  `approve_plan(id)` / `reject_plan(id, reason)` / `set_gate_message(id, message_id)`

## 5. 実装順序 (両 spec の関係)

1. `2026-07-05-planner-position-plan-context.md` (優先度高・独立) — 先行可
2. 本 spec finance 側 (F-1→F-7 の順、F-4 は独立レビュー厚め)
3. 本 spec discord_bot 側 (finance API が生えてから)
4. paper で gate ON → スコアカード運用開始

## 6. テスト観点

- **publish 分岐** (F-6): gate ON で create は requires_replan のまま・最後の publish
  だけ pending_approval になる (write 順序 = orphan 防止が gate ON でも保たれる)。
  gate OFF で現行挙動不変 (新列すべて NULL)
- **gate 決定 helper** (F-2b): try_decide_gate が status + gate_decision +
  gate_decided_at + gate_reason を単一 UPDATE で残す。並行 approve+reject で
  片方 409。expired 済み plan への approve は 409 (rowcount 0)
- **unanswered**: pending 中に entry 成立 → stamp → TTL 到達で expired +
  gate_decision='unanswered' + cf 行記録 (triggered_at=stamp 時刻)。
  stamp なしで TTL 到達 → expired + unanswered、cf 行なし
- **rejected の追跡**: reject 時 stamp 済 → 即 cf 行 / stamp なし → reject 後の
  entry 初成立で cf 行 (1回だけ・status 不変)。**invalidation latch**: rejected で
  invalidation 成立 → cf_state='invalidated'、その後 entry が成立しても cf 行を
  記録しない
- **pending の invalidation**: pending_approval → invalidated (real 遷移) +
  gate_decision='unanswered'。承認不能になること。stamp 済なら cf finalize される
- **finalize の原子性・crash recovery** (codex 2巡目 High):
  finalize_cf_trigger が claim + shadow 行 + hindsight enqueue を単一 tx で行う
  (途中クラッシュで cf_state だけ進んだ孤児が出ない)。status=rejected/expired/
  invalidated + cf_state='would_trigger' + shadow 行なし、を人工的に作る →
  次の watch tick で finalize が再実行され cf 行 + hindsight が揃う。二重実行しても
  行が増えない (claim 冪等 + UNIQUE)
- **F-7 の NULL セマンティクス**: gate OFF plan (gate_decision NULL) が実性能集計に
  含まれること (`IS NULL OR NOT IN` フィルタの検証)
- **UNIQUE 共存**: stamp 済み pending を承認 → active → real trigger が正常記録
  される (cf 行が先に書かれていないので UNIQUE 衝突しない)。cf 行記録済み plan に
  real trigger 経路が到達しないこと (rejected/expired は watch の active 集合にいない)
- **cf 経路の分離**: order_intent 不作成、live mode + broker 注入でも執行されない、
  decision type が plan_cf_trigger、hindsight が過去時刻 triggered_at でも拾うこと
- supersede 拡張: pending_approval が置換されること、rejected は置換対象外
  (terminal のまま)。superseded pending は cf 行を起こさない
- API: 認証、409 系、reason の永続化、gate_message の冪等性・404、
  detail の gate_decision 反映、posted_within_hours の窓・status 不問の検証、
  APIState.orchestrator_store 注入 (未注入時に 503 等で落ちないこと)
- metrics: 3ラベル分割の件数・pnl_r 集計。**real 集計から cf 行が除外される**
  (gate_decision NOT IN ('rejected','unanswered') フィルタ)
- bot 側 (手動確認中心): persistent view の再起動生存、二重クリック、
  channel 権限 (`cog_group` = finance の既存ゲート)、再起動後の gate_message_id 突合
