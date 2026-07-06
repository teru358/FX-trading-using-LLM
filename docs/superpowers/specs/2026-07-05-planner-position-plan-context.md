# Planner への建玉・既存 plan 参照配線 設計 spec

日付: 2026-07-05
状態: 設計確定 (実装前)
優先度: **高** (ユーザー指定)
関連: `2026-06-20-orchestrator-agent-loop-design-v2.md` §7 (decision context),
`2026-07-04-consolidated-roadmap.md` §1 (旧問題1: シグナルなしの同方向重ね),
`2026-07-05-discord-approval-gate.md` (独立 spec、依存なし・並行可)

---

## 1. 目的と方針

旧システム問題1「ロングトレンド時にシグナルなくロングを重ねる」への v2 恒久対応。

**scale-in は全否定しない**(ユーザー確認済 2026-07-05)。トレンドに乗る追加エントリーは
正当。否定するのは「建玉の存在を知らずに・新たなシグナルなしに・むやみに」乗ること。

したがって対処は**ハードガードの追加ではなく、判断材料の配線**:

- planner に建玉 (side/entry/含み損益/MFE) と既存 plan を渡し、追加エントリーには
  「新たなシグナルの明示」を prompt で要求する
- 決定的な最終壁は既存の broker 層 `max_positions_per_pair: 2` (scale-in 1回許容) を
  **変更しない**。装置を重ねず、判断は planner・強制は broker の一層ずつ
- planner が建玉を踏まえたかは構造化フィールド (P-2b) と snapshot 保存 (P-5) で
  機械的に検証可能にする (「治す」でなく「測る」の哲学と整合)。ただし建玉取得
  失敗時だけは prompt に依存せず決定的に direct_hold へ倒す (P-4)

## 2. 現状 (2026-07-05 時点の事実)

- `DecisionContextBuilder._assemble` (context_builder.py:145): `position` は
  `_empty_position()` **固定 stub** `{side, entry, size, pnl, mfe_r}` 全 None
- `recent_decisions/orders/exits/trade_stats` も全て空 stub (本 spec のスコープ外)
- planner prompt の `_compact_context` (planner_agent.py:96-102) は **position キーを
  既に LLM へ渡している** — 中身を実データにすれば prompt 側の配線は不要
- 既存 plan: planner は自分が置換しようとしている active plan を見ずに planning
  している。supersede (pair 単位 active 最大1, planning_pipeline.py:292) は構造的
  ガードとして機能済みだが、planner は「維持 vs 置換」を情報なしで判断している
- 建玉データ: `PositionManager` (StateStore 経由) の `Order` に direction ("buy"/"sell"),
  entry_price, position_size, opened_at, max_favorable_r, initial_risk_price_distance,
  is_scale_in, signal_reason が揃っている

## 3. 構造変更

### P-1: position provider の注入と配線

`DecisionContextBuilder` に `position_provider: Callable[[str], list[dict]] | None` を
追加 (news_provider / risk_state_provider と同じ注入パターン)。**provider は raw の
open position (Order 由来 dict) の list を返すだけ**とし、pnl_r 等の整形は builder 側で
build 時の `quote.mid` を使って行う (codex High#2: provider に quote を渡さない設計で
pnl_r を算出するため、整形責務を builder に置く)。bootstrap で構築:

- ProtectionWorker と同様 self-contained に `PositionManager(StateStore(config.state_dir))`
  を planning 用に1つ作り、**呼び出し毎に reload()** してから該当 pair の open
  positions を読む (planning は 60s 周期なのでコスト無視できる)
- context の `position` ブロック形状 (stub の単一 dict から拡張):

```json
{
  "count": 1,
  "items": [
    {
      "direction": "long",          // buy→long / sell→short に正規化
      "entry_price": 150.20,
      "size": 10000,
      "opened_at": "2026-07-03T09:15:00",
      "pnl_r": 0.8,                 // (mid−entry)/initial_risk_price_distance, 符号は方向補正
      "mfe_r": 1.2,                 // Order.max_favorable_r
      "is_scale_in": false,
      "entry_reason": "..."         // Order.signal_reason (先頭 200 字で切る)
    }
  ]
}
```

- `pnl_r` は builder が build 時の quote.mid で算出。`initial_risk_price_distance == 0`
  なら null
- provider 例外時は `{"count": null, "items": [], "status": "unavailable"}` + warning
  ログ (1材料の失敗で cycle を落とさない)。ただし**このケースの安全は prompt に
  依存させず P-5 の決定的 fail-safe で処理する** (codex High#1)

### P-2: current_plan ブロックの追加

context に `current_plan` キーを新設。orch_store から該当 pair の
**status = active** の plan (最大1件、supersede 保証) を要約 (codex Medium#1:
`pending_approval` は現行 PLAN_STATUSES に存在しない。approval gate spec の実装時に
対象へ追加する — gate spec 側に記載):

```json
{
  "plan_id": 123,
  "status": "active",
  "direction": "long",
  "entry_summary": "price_at_or_below 149.80",   // entry_conditions_json の短縮表記
  "expires_at": "2026-07-05T20:00:00",
  "created_at": "2026-07-05T12:00:00"
}
```

plan なしなら `null`。`_compact_context` に `current_plan` を追加 (planner /
ExecutionOpinionAgent 両方に届く)。

### P-2b: 構造化 scale-in フィールド (codex Medium#3)

「新シグナル根拠がある」の検証を reasoning の自然文判定に依存させないため、
`ExecutionPlanDraft` (LLM 出力 schema) に構造化フィールドを追加する:

- `scale_in: bool` — LLM も申告するが、**正本は pipeline の決定的導出**
  (codex plan review High): `scale_in = 建玉 items に draft.direction と同方向が存在`。
  LLM 申告が導出値と食い違えば導出値で上書きする (どちら向きの誤申告も矯正)
- `new_signal_evidence: str | null` — scale_in (導出値)=true のとき必須。
  evidence 必須は pipeline の決定的 gate が一元処理する (schema は型検証のみ)。
  scale_in=true + evidence 空も他の不備と同じ feedback 再起案経路に乗る:
  redraft 予算が残っていれば 1 回再起案、尽きていれば決定的 reject
- 型検証は厳格に: scale_in は JSON bool のみ (文字列 "false" は SchemaParseError)、
  new_signal_evidence は null | str のみ

永続化は trade_plans への nullable 列 2 本 (`scale_in`, `new_signal_evidence`、
idempotent ALTER)。集計は SQL だけで「scale-in plan のうち根拠記述がある比率」を
出せる。導出が決定的なので「同方向建玉あり plan で scale_in=false」は構造的に
存在しない (LLM の見落としを測る場合は申告値と導出値の不一致率を agent_output
の structured_payload から集計する)。

**意味論**: planner が current_plan を見て direct_hold を返す = 既存 plan の維持
(supersede は新 plan 作成時のみ発火するので現行コードのままこの意味論が成立する)。
新 plan を返す = 置換の明示的判断。「上書きか新規か」は planner の判断に昇格する。

### P-3: prompt 指針の追加

`_horizon_guidance` と同じ場所 (execution_opinion_agent.py、planner が import) に
position 指針を追加。内容 (要旨):

- 同 pair に建玉がある状態で**同方向**の plan を出す場合、それは scale-in である。
  `scale_in: true` を立て、entry 時と異なる**新たなシグナル**を `new_signal_evidence`
  に記述すること。既存建玉の entry_reason と同じ根拠の再掲は理由にならない
- 建玉と**逆方向**の plan は実質的なドテン提案。既存建玉が invalidation に近い等の
  根拠を示すこと
- current_plan が存在し前提が変わっていなければ direct_hold (plan 維持) を選ぶこと。
  置換は前提の変化を reasoning で示す

(position 取得失敗時の挙動は prompt でなく P-5 で決定的に処理する)

### P-4: 決定的 fail-safe — position 取得失敗時は planning しない (codex High#1)

`position.status == "unavailable"` のときは **LLM を呼ばず direct_hold に倒す**
(pipeline 入口の決定的分岐)。理由:

- 建玉取得失敗時こそ「建玉を知らずに重ねる」が再発する瞬間であり、その安全を
  prompt (確率的) に依存させない
- risk_state provider 失敗時の fail-safe (楽観に倒さない) と同じ思想
- LLM 呼び出しの節約にもなる。direct_hold の reasoning に
  "position unavailable — planning skipped (fail-safe)" を記録し、頻発するなら
  provider 側の障害として気づける

RiskGateWorker への position チェック追加は行わない (装置を重ねない。執行時の
最終壁は broker の max_positions_per_pair が既に担う)。

### P-5: snapshot への保存 — 検証可能性 (codex Medium#2)

現行 decision_snapshots は quote_json / technical_ref / news_ref のみで、LLM が見た
建玉・既存 plan を後から再現できない。nullable 列 2 本を追加 (idempotent ALTER):

- `position_json` — context に入れた position ブロックをそのまま保存
- `current_plan_json` — 同 current_plan (null 可)

これで「そのとき LLM は建玉を知っていたか」が snapshot から機械的に復元でき、
検証が reasoning 目視に依存しない。

### P-6: 変更しないもの (明示)

- `max_positions_per_pair: 2` — 最終壁として現状維持。planner の判断品質で連続発注が
  収まるかを先に測る。収まらなければそのとき初めて 1 を検討
- supersede の仕組み (pair 単位 active 最大1) — 不変
- `recent_orders/exits/trade_stats` の stub — 別スコープ (必要性が測定で示されてから)

## 4. 検証 (shadow / paper で)

- **合格条件は SQL で機械判定**: scale_in=true の plan は new_signal_evidence が
  非空であること (P-2b の構造化フィールド)。reasoning 目視は補助
- scale_in は決定的導出なので突合不要。LLM の建玉認識品質は「申告値 vs 導出値の
  不一致率」で測る (snapshot position_json + draft 申告から)
- 建玉保有中 pair への同方向 plan 作成頻度 before/after
- 5問題スコアカード問題1の判定材料

## 5. テスト観点

- position provider: 建玉なし→count 0 / items 空、建玉あり→正規化 (buy→long)、
  pnl_r 算出は builder 側 (方向別符号・risk 0 で null・quote.mid 使用)、
  provider 例外→unavailable ブロック
- P-4 fail-safe: unavailable → LLM 不呼び出しで direct_hold 記録 (pipeline 入口分岐)
- current_plan: active あり / なし の2通り、entry_summary 短縮
- P-2b: scale_in=true + evidence 空 → 再起案経路、position 空 + scale_in=true →
  false に矯正、trade_plans への永続化
- P-5: snapshot に position_json / current_plan_json が保存・復元できること
- _compact_context に position 実データと current_plan が乗ること
- prompt: guidance 行が horizon 指針と併存すること (day/swing 両方)
- 既存テスト回帰: position stub 前提のテストは新形状に追従 (空のときの形状は
  count 0 + items [] に変更、旧 {side: None, ...} からの移行)
