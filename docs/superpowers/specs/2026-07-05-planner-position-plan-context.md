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
- planner が建玉を踏まえたかは reasoning_summary に残る → shadow 期間に検証可能
  (「治す」でなく「測る」の哲学と整合)

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

`DecisionContextBuilder` に `position_provider: Callable[[str], dict] | None` を追加
(news_provider / risk_state_provider と同じ注入パターン)。bootstrap で構築:

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

- `pnl_r` は build 時の quote.mid で算出。`initial_risk_price_distance == 0` なら null
- provider 例外時は fail-safe: `{"count": null, "items": [], "status": "unavailable"}`
  + warning ログ。risk_state provider と同じ思想 (1材料の失敗で cycle を落とさない)。
  prompt 指針 (P-3) が「position 不明時は追加エントリーを提案しない」を担保する

### P-2: current_plan ブロックの追加

context に `current_plan` キーを新設。orch_store から該当 pair の
status ∈ {active, pending_approval} の plan (最大1件、supersede 保証) を要約:

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

**意味論**: planner が current_plan を見て direct_hold を返す = 既存 plan の維持
(supersede は新 plan 作成時のみ発火するので現行コードのままこの意味論が成立する)。
新 plan を返す = 置換の明示的判断。「上書きか新規か」は planner の判断に昇格する。

### P-3: prompt 指針の追加

`_horizon_guidance` と同じ場所 (execution_opinion_agent.py、planner が import) に
position 指針を追加。内容 (要旨):

- 同 pair に建玉がある状態で**同方向**の plan を出す場合、それは scale-in である。
  entry 時と異なる**新たなシグナル** (新しい技術的根拠・材料) を reasoning に明示
  すること。既存建玉の entry_reason と同じ根拠の再掲は scale-in の理由にならない
- 建玉と**逆方向**の plan は実質的なドテン提案。既存建玉が invalidation に近い等の
  根拠を示すこと
- `position.status == "unavailable"` (取得失敗) のときは追加エントリーを提案しない
- current_plan が存在し前提が変わっていなければ direct_hold (plan 維持) を選ぶこと。
  置換は前提の変化を reasoning で示す

### P-4: 変更しないもの (明示)

- `max_positions_per_pair: 2` — 最終壁として現状維持。planner の判断品質で連続発注が
  収まるかを先に測る。収まらなければそのとき初めて 1 を検討
- supersede の仕組み (pair 単位 active 最大1) — 不変
- `recent_orders/exits/trade_stats` の stub — 別スコープ (必要性が測定で示されてから)

## 4. 検証 (shadow / paper で)

- reasoning_summary に建玉言及があるか (建玉あり pair の plan について目視 + 後日集計)
- 建玉保有中 pair への同方向 plan 作成頻度 before/after
- 5問題スコアカード問題1の判定材料になる: 「同方向 plan には新シグナル根拠が
  reasoning に必ずある」が合格条件

## 5. テスト観点

- position provider: 建玉なし→count 0 / items 空、建玉あり→正規化 (buy→long)、
  pnl_r 算出 (方向別符号・risk 0 で null)、provider 例外→unavailable ブロック
- current_plan: active / pending_approval / なし の3通り、entry_summary 短縮
- _compact_context に position 実データと current_plan が乗ること
- prompt: guidance 行が horizon 指針と併存すること (day/swing 両方)
- 既存テスト回帰: position stub 前提のテストは新形状に追従 (空のときの形状は
  count 0 + items [] に変更、旧 {side: None, ...} からの移行)
