# Discord 承認ゲート (approval gate) 設計 spec

日付: 2026-07-05
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
pipeline 作成 (gate ON)
   └→ pending_approval ──承認──→ active ──成立──→ triggered (以降現行どおり)
            │        └──却下──→ rejected   (terminal・反実仮想追跡は継続)
            └──放置──→ expired  (TTL、既存 sweep)
```

- position-context spec (P-2) の `current_plan` ブロックの対象を
  {active} → {active, pending_approval} に拡張する (返答待ち plan も planner から
  見えるようにする。status 追加とセットで本 spec 側の作業)
- 遷移は既存 `try_claim_plan_status` を使う:
  approve = claim(active, from=pending_approval) / reject = claim(rejected, from=pending_approval)。
  二重クリック・承認と却下の競合は rowcount 排他で自然解決、負けた側は 409
- gate OFF (既定) は現行どおり active で作成 — **挙動不変**

### F-2: trade_plans への nullable 列追加 (idempotent ALTER)

| 列 | 内容 |
|---|---|
| `gate_decided_at` | 承認/却下の時刻 (放置は NULL) |
| `gate_reason` | 却下理由 自由記述 (任意) |
| `gate_message_id` | Discord メッセージ ID (bot 再起動後の突合用) |

ラベルは status から復元する (approved=active 以降 / rejected / unanswered=
pending のまま expired)。新テーブルは作らない。

### F-3: supersede の対象拡張

`supersede_active_plans` の対象を status ∈ {active, **pending_approval**} に拡張。
新 plan 作成時に返答待ち plan も置換される (G-6)。置換された pending plan は
superseded であり rejected ではない (人間の判断なしラベル)。

### F-4: watch の反実仮想追跡 (本体・要厚めレビュー)

watch loop の評価対象を active のみ → **active + pending_approval + rejected** に拡張。

- 非 active plan は **trigger 記録のみ**: shadow_trigger 行を記録し、status claim
  しない・執行経路に入らない・order_intent を作らない。既存の hindsight poll が
  spread 込みで採点する (機構は現行のまま)
- 執行境界は不変: live 執行と triggered claim は active のみ。rejected が誤執行
  される経路は構造的に存在しない
- **dedupe**: 非 active plan は status が動かないため、trigger 記録は plan_id 単位で
  1回に制限する (「最初に条件成立した瞬間」が反実仮想のエントリー点)。
  shadow_triggers に plan_id の既存記録があるかを事前確認し、あればスキップ
- shadow_trigger に記録時点の plan status (pending_approval / rejected) を残し、
  集計でラベル分割できるようにする (列追加 or 既存 JSON への付与は実装時判断)

### F-5: API 3本 (既存 FastAPI 8811 / X-API-Key)

| endpoint | 動作 |
|---|---|
| `GET /orchestrator/plans?status=pending_approval` | pending 一覧 (plan_id, pair, direction, entry_summary, SL/TP, expires_at, created_at, reasoning 要約) |
| `POST /orchestrator/plans/{id}/approve` | claim 成功→200 {status: active}、失敗→409 (決定済み/期限切れ) |
| `POST /orchestrator/plans/{id}/reject` body `{reason?: str}` | claim 成功→200、失敗→409。reason は gate_reason へ |

### F-6: config

`orchestrator.approval_gate: bool = False` (schema 既定 OFF = 挙動不変)。
ON のとき pipeline の plan 最終 status を active でなく pending_approval にする
(変更点は作成時 status の1箇所)。

### F-7: metrics / daily summary

`get_shadow_metrics_raw` と daily summary に gate ラベル別の行を追加:
approved / rejected / unanswered の件数・trigger 率・hindsight 平均 pnl_r。

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
  再起動時は pending 一覧 GET と `gate_message_id` で突合し、未投稿分だけ再投稿
- polling で pending から消えた plan は plan 詳細 (または一覧 API の decided 込み
  レスポンス) で結末を判定してメッセージ edit
- `FinanceClient` 追加メソッド: `pending_plans()` / `approve_plan(id)` /
  `reject_plan(id, reason)`

## 5. 実装順序 (両 spec の関係)

1. `2026-07-05-planner-position-plan-context.md` (優先度高・独立) — 先行可
2. 本 spec finance 側 (F-1→F-6 の順、F-4 は独立レビュー厚め)
3. 本 spec discord_bot 側 (finance API が生えてから)
4. paper で gate ON → スコアカード運用開始

## 6. テスト観点

- status 遷移: approve/reject の claim 排他 (並行 approve+reject で片方 409)、
  gate OFF で現行挙動不変、pending の TTL sweep → expired
- supersede 拡張: pending_approval が置換されること、rejected は置換対象外
  (terminal のまま)
- 反実仮想 watch: 非 active plan の trigger 記録 (1回だけ)、status 不変、
  order_intent 不作成、live mode でも執行されない、hindsight が拾うこと
- API: 認証、409 系、reason の永続化
- metrics: 3ラベル分割の件数・pnl_r 集計
- bot 側 (手動確認中心): persistent view の再起動生存、二重クリック、
  channel 権限 (`cog_group` = finance の既存ゲート)
