# plan 品質バグ修正 — RR 決定的検算 + draft parse 救済 (Phase 1-1)

**Date:** 2026-07-16
**Status:** 設計確定 (ユーザー承認済)。実装未着手。
**親文脈:** orchestrator version2 ロードマップ (`2026-07-04-consolidated-roadmap.md` §2 Phase 1-1)。
2026-06-30 観測の reject 19 件 (RR 計算ミス / SL/TP 構造 / SchemaParseError 未知 invalidation type) への恒久対応。
**関連:** `2026-06-20-orchestrator-agent-loop-design-v2.md` (設計正本 §5.2/§5.3/§13#7)。

---

## 0. 背景と問題

2026-06-30 (TZ バグ修正直後・旧 14B モデル構成) の Fiosracht 観測で、plan が risk gate まで到達するも reject 19 件・発注 0 件。原因調査 (2026-07-16) で以下のコード上の構造問題を特定した。

### 問題 1: risk gate が LLM 申告 rr を無検証で信用している

`RiskGateWorker._fixable_issues` (`src/orchestrator/risk_gate.py`) は `draft.action["rr"]` (LLM 自己申告値) を `min_rr` と比較するだけで、sl/tp/entry から実 RR を再計算していない。穴は 2 方向:

- (i) LLM が計算を誤り rr < min_rr を申告 → 本来通るべき draft が fixable reject (観測された reject の一部)
- (ii) **rr を過大申告すれば実 RR が低くても通過する** — 偽 pass。reject より深刻で、以後の検証データ (スコアカード) を汚す
- (iii) **live final gate も同じ pre_check を使うため、trigger 時の実行価格が RR 検証に反映されない** — breakout オーバーシュートで実行 RR が計画 RR から大きく劣化しても、SL/TP side 条件さえ満たせば発注まで通る (レビュー High#2)

旧 cycle 側には ATR 適用後の実測 RR 検算がある (`src/cycles/trading.py:217-228`)。orchestrator gate に同等の決定的検算がない。

### 問題 2: draft の SchemaParseError に redraft 救済がない (非対称)

LLM が invalidation type に vocabulary 外の値 (`boj_intervention_signal` 等) を出すと `ExecutionPlanDraft.from_llm_json` が `SchemaParseError` を raise し、`PlanningPipeline.run` の `_FAILSAFE_EXC` で **run 全体 failed・redraft なし** で終わる。

一方、RR 不足や SL/TP side ミス (fixable) には redraft 1 回の救済がある。LLM にとって同程度に「直せる」ミスである vocabulary 逸脱だけ救済ゼロ、という非対称。

### 問題 3: min_rr が config 未接続

gate の `min_rr` は既定 1.5 だが、`bootstrap.py` の `RiskGateWorker(...)` 構築時に config から渡しておらず実質ハードコード。day horizon プロンプトは「Keep RR >= 2」を指示しており、「2 を狙わせ 1.5 で切る」マージン構造自体は妥当だが、day/swing で調整できない。

### 環境注記

- 観測時点 (06-30) は旧モデル構成。現在は全 agent を Qwen3.6-35B-A3B で運用 (news 用 9B/14B は不使用 — 3.6 の方が処理が軽く安定するため)。発生率は変わっている可能性があるが、(ii) の偽 pass と非対称の構造問題はモデル品質と無関係に塞ぐ価値がある。
- 稼働環境の reject 分布の再計測は動作確認完了後に別途行う (本 spec の前提ではない)。

---

## 1. 設計判断 (確定 4 点)

| # | 論点 | 決定 |
|---|---|---|
| D-1 | RR 導出の entry 基準 | **phase で分離** (レビュー R2 High#1 反映): planning/coerce = price 系 entry_condition の value のみ (price 条件が無い場合のみ実行価格 fallback) / live final gate・shadow precheck = 上記候補 + trigger 時実行価格 (long=ask / short=bid、無ければ mid)。**候補ごとに RR を計算し最小値を採用** (R1 High#1)。phase は `pre_check(..., include_executable_price=)` で呼び出し元が明示する — 詳細は §2.A |
| D-2 | 申告 rr の扱い | **導出可能なら常に導出値で置換** (レビュー Medium#3 反映)。申告値は agent_outputs に残し、plan には導出値を保存。乖離 10% 超は INFO ログ (不一致メトリクスの閾値としてのみ使用) |
| D-3 | スコープ | **SchemaParseError 救済も本 spec に含める** (同じ draft ループの修正、redraft 予算 max_redraft=1 を共有) |
| D-4 | min_rr | **config 化して接続** (既定 1.5 = 現行同値、挙動互換)。有限・正数の設定値検証付き (R1 Medium#5)。**gate は bootstrap で 1 個だけ構築し pipeline と runtime で共有する** (R2 High#2 — 現行は二重構築で runtime 側フォールバックが config 未接続) |
| D-5 | 数値検証 | **schema 構築時に有限 float へ正規化**: action の sl/tp/rr (R1 Medium#4) に加え、`_opt_float` (EntryCondition/InvalidationCondition の value/value_pips が通る共通ヘルパ) にも bool/NaN/Inf 拒否を追加 (R2 High#3)。str 数値は変換許容。違反は SchemaParseError → 2.D の redraft 救済に乗る。`derive_rr` 側も非有限候補を防御 (rr=0.0 扱い = reject 方向) |

---

## 2. 変更内容

### 2.A RR 導出関数 (risk_gate.py)

```python
def derive_rr(
    draft: ExecutionPlanDraft, quote: dict | None, *, include_executable_price: bool,
) -> float | None:
    """sl/tp/entry 候補から reward/risk 比を決定的に導出する。導出不能なら None。

    entry 候補:
      - price 系 entry_condition (price_at_or_below / price_at_or_above /
        breakout_above / breakout_below) の value 全部 — 常に評価
      - 実行価格 (executable price): long → quote.ask / short → quote.bid、
        無ければ quote.mid — include_executable_price=True のとき、**または**
        price 系候補がゼロのとき (fallback) に追加
    候補ごとに rr = reward / risk を計算し、**最小値を採用** (保守則):
      long : reward = tp - entry, risk = entry - sl
      short: reward = entry - tp, risk = sl - entry
    退化候補 (risk <= 0: entry が SL の防御側に無い / reward < 0: entry が TP を
    超えている) は「その entry では成立しない」= rr 0 相当なので **0.0 として
    min に参加させる** (黙って除外すると悪い候補ほど無視される)。
    非有限候補 (NaN/Inf、entry/sl/tp のいずれか) も **0.0 扱い** (reject 方向 =
    安全側。NaN 比較の False 化による偽 pass を防ぐ、R2 High#3)。
    None 条件: 候補ゼロ (price 条件なし & 実行価格なし) / sl or tp 欠落 /
      **include_executable_price=True かつ実行価格が取得不能** (R4 High#1)。
    """
```

- **実行価格必須化 (R4 High#1)**: `include_executable_price=True` (live final gate / shadow precheck) は「trigger 時実行価格で検証する」のが目的。実行価格が欠落・非数値で取れないとき、price 条件候補 (計画 RR) だけで pass すると breakout オーバーシュートを見逃す。よって **`include_executable_price=True` で `_executable_price` が None なら derive_rr 全体を None (underivable → gate で fixable reject)** に倒す。「trigger 時に実勢価格を確認できないなら発注しない」= spread unknown を reject にしたのと同じ思想。quote 回復後の次 tick で再評価される。planning phase (False) は実行価格を使わないので影響なし (price 条件ゼロの fallback 時のみ実行価格を見るが、その場合も取れなければ従来通り候補ゼロ → None)。

- **最小値採用の理由 (R1 High#1)**: long で entry が SL に近いほど risk 分母が小さく RR は**大きく**なる。「SL に最も近い entry = 最悪」は逆。最悪ケースは候補ごとに RR を出した上での min でしか正しく取れない。
- **phase 分離の理由 (R2 High#1)**: 押し目 plan (long, 現在 ask=151, entry 条件=149.5, SL=148.5, TP=151.5) では現在価格 RR=0.2 だが、entry 条件はまだ成立しておらず約定は条件成立後 — planning 時に実行価格を候補に含めると正常な押し目 plan (計画 RR 2.0) を誤 reject し、プロンプトの「pullback/retest 優先」指針とも衝突する。trigger セマンティクス (watch_evaluator: `price_at_or_below` は mid<=value で成立) から、押し目系の trigger 時実行価格は条件値の**有利側**にあり計画 RR を下回らない。実行 RR が計画を下回り得るのは breakout オーバーシュート = **trigger 時** のみ。よって:
  - **planning gate / coerce**: `include_executable_price=False` — 計画 RR (price 条件が無い draft のみ実行価格 fallback。underivable を避けるため)
  - **live final gate / shadow precheck**: `include_executable_price=True` — min(計画 RR, trigger 時実行 RR)。breakout オーバーシュート (例: breakout=150 / SL=149 / TP=152 で trigger 時 ask=151.8 → 実行 RR ≈ 0.07) を reject。shadow precheck も「発注していたら」の判断品質記録なので live と同基準
- `pre_check` にも同名 keyword 引数を追加し、呼び出し元が phase を明示する: pipeline (planning) = False / runtime の live final gate・shadow precheck = True。既定値は**設けない** (呼び出し元に選択を強制 — 暗黙 default はこのバグの再発経路)。
- 約定想定は buy=ask / sell=bid (spread 込み)、bid/ask 欠落 provider では mid フォールバック。
- 純関数。module-level に置き pipeline (coerce) からも import する — 二重実装しない。
- `_ENTRY_PRICE_TYPES` (schemas.py の price 系 4 種) を判定に使う。`spread_below` / `technical_status_is` は entry 価格を持たないので無視。

### 2.A′ 数値正規化 (schemas.py) — R1 Medium#4 + R2 High#3

**(a) action の sl/tp/rr** — `ExecutionPlanDraft` 構築時に正規化する:

- 値が存在する場合: `float()` へ変換し、**有限であること** (`math.isfinite`) を検証。bool は数値として拒否 (`isinstance(v, bool)` を先に弾く)。str 数値 (`"149.0"`) は変換を許容 (ローカル LLM の揺れ対策)。
- 変換不能 / NaN / Infinity → `ValueError` → 既存の except で `SchemaParseError` 化 → **2.D の redraft 救済に自然に乗る** (feedback にエラー内容が入る)。
- sl / tp / rr の**欠落は正規化では拒否しない** (欠落の扱いは gate の責務: `missing sl/tp` issue)。
- action 内の他キー (size_policy / comment 等) は従来通り未検証。
- `_build_execution_draft` (runtime の draft 復元) はコンストラクタ直呼びなので、正規化は `__post_init__` に置く — 保存済み plan (旧データ) に str 数値が残っている可能性がある復元経路もカバーする。frozen でない dataclass なので action dict 差し替えで足りる。

**(b) `_opt_float` の強化 (R2 High#3)** — EntryCondition / InvalidationCondition の value / value_pips が通る共通ヘルパ `_opt_float` に bool 拒否 + `math.isfinite` 検証を追加する。entry 条件の NaN 候補が `derive_rr` の min に混入すると `NaN < min_rr` が False になり hard gate を偽通過するため。`_opt_float` は SchemaParseError を raise する既存契約なので、違反はこれも redraft 救済に乗る。

**(c) 深層防御** — quote (bid/ask/mid) は schema 層を通らないため、`derive_rr` 内で非有限の候補 (entry/sl/tp のいずれかが NaN/Inf) を rr=0.0 扱いにする (§2.A)。reject 方向 = 安全側。`QuoteSnapshot` 自体への検証追加はスコープ外 (§3) — gate の防御で偽 pass は塞がる。

### 2.B gate の RR チェックを導出ベースに変更 (risk_gate.py)

`pre_check` / `_fixable_issues` に `include_executable_price: bool` keyword 引数 (既定なし = 必須) を追加し、RR 節を差し替え:

- `derived = derive_rr(draft, context.get("quote"), include_executable_price=include_executable_price)`
- `derived is None` → issue `"rr underivable (no entry candidate)"` を fixable に追加 (sl/tp 欠落時は既存の `missing sl` / `missing tp` issue が先に立つので、この issue は entry 起因のケースを主に拾う)。**楽観通過させない** — spread unknown を fixable reject にしたのと同じ思想。
- `derived < min_rr` → issue `f"derived rr {derived:.2f} below min {min_rr} (claimed {claimed})"`。claimed (申告値、None なら "none") を併記し redraft feedback を具体化する。退化候補・非有限候補 (rr=0.0) が min を引き下げたケースもこの分岐で reject される。
- 申告 `action["rr"]` は gate では比較に使わない。`missing rr` issue は廃止 (申告は任意の参考値になる)。
- **呼び出し元の変更 (R2 High#1)**: pipeline (planning) は `include_executable_price=False`、runtime の live final gate (`_execute_live_trigger`) と shadow precheck (`_shadow_risk_precheck`) は `True` を渡す。runtime の 2 呼び出し行に keyword を足すだけで、reject 後の遷移 (fixable → intent=`abandoned`・plan=`invalidated`・replan) は既存のまま不変。
- **fake gate 互換**: テストの `_GatePass`/`_GateReject` (test_taskf_live_execution_helpers) は `pre_check(draft, context)` シグネチャ — keyword 追加に合わせ `**kwargs` を受けるよう更新する。
- **spread の有限性検証 (R4 Medium#2)**: `_fixable_issues` の spread 節は `spread` を float 化した後に `math.isfinite` を検証する。現状 `spread is None` は reject するが、NaN/Inf/非数値 str は `spread / pip_size` → `NaN > max` 常に False で **spread ガードを黙って通過** (fail-silent、min_rr で塞いだのと同型)。非数値 str は除算で TypeError → gate クラッシュ。対処: `spread is None` → `"spread unknown"` は維持しつつ、`float()` 変換失敗 or `not math.isfinite` → `"spread invalid"` の fixable reject に倒す。upstream (`mt5_ohlcv_fetcher.py` の `float()` のみ) では `"nan"` 文字列を弾けないため gate 側の防御が最終壁。

### 2.C 申告 rr の coerce (planning_pipeline.py)

scale_in coerce (`draft.scale_in != same_dir` ブロック) の直後、`final_decision` 呼び出しの前に追加:

- `derived = derive_rr(draft, context.get("quote"), include_executable_price=False)` — coerce は planning phase なので計画 RR (§2.A の phase 分離と一貫。plan に保存する rr は計画値であるべきで、planning 時点の一時的な実勢を焼き込まない)
- `derived is not None` なら**常に置換** (レビュー Medium#3: D-2「plan には導出値を保存」と条件付き置換は矛盾するため、置換は無条件・閾値はログのみに使う):
  - `draft.action` を複製し `action["rr"] = round(derived, 2)` で置換 (draft は `replace(draft, action=new_action)` — action dict の共有ミューテーションを避ける)
  - 申告 rr が None または相対乖離 > 10% (`abs(claimed - derived) > 0.10 * derived`) のときのみ INFO ログ `[ORCH] rr claim overridden for %s: llm=%s derived=%.2f` (不一致メトリクスの発火閾値)
- `derived is None` の場合は coerce しない (gate 側 2.B が reject する)。
- **順序契約 (既存 scale_in と同じ)**: `_persist_opinion` は coerce の**前**に実行済みであること — 申告値が agent_outputs.structured_payload.action.rr に残り、plan の action_json.rr (導出値) との不一致率を SQL で測定できる (スコアカード④ confidence 較正系の材料)。

### 2.D draft parse 失敗の redraft 救済 (planning_pipeline.py)

draft ループ内の `await self._exec.draft(...)` を try/except で包む:

```python
try:
    draft = await self._exec.draft(..., revision_feedback=feedback)
except SchemaParseError as exc:
    # 監査痕跡 (レビュー Low#6): parse 失敗 draft は _persist_opinion に到達しない
    # ため、構造化ログで pair / 例外要約 / 何回目かを残す (schema 逸脱率の追跡用)。
    logger.warning(
        "[ORCH] draft schema parse failed for %s (attempt %d): %s",
        pair, redraft_count + 1, exc,
    )
    if redraft_count < max_redraft:
        redraft_count += 1
        feedback = [
            f"Previous draft failed schema validation: {exc}. "
            "Use ONLY the condition vocabularies listed in the schema."
        ]
        continue
    raise  # 予算切れ → 従来通り _FAILSAFE_EXC で failed (挙動互換)
```

- 監査は WARNING 構造化ログのみとし、agent_outputs への行追加はしない (parse 失敗時は raw text が schema 層で捨てられており永続化の価値が薄い + output_type 語彙の拡張は消費側へ波及するため)。schema 逸脱率はログ集計で追跡する。

- 救済対象は **ExecutionOpinionAgent.draft の SchemaParseError のみ**。`scan_opportunity` / `final_decision` の parse 失敗は従来通り即 failed (救済なし) — planner 側の parse 失敗は draft の再起案では直らないため。
- redraft 予算は既存 `max_redraft = 1` を共有 (scale-in evidence / planner revise / risk fixable と同じ予算プール)。parse 救済で 1 回消費した後に fixable reject が出れば、そのまま reject 終端 (予算追加はしない — ロードマップ §3 決定待ち「max_redraft」の論点はそのまま)。

### 2.E min_rr の config 化 (schema.py / bootstrap.py / settings.yaml.example)

- `OrchestratorEntryConfig` (schema.py) に `min_rr: float = 1.5` を追加 (`spread_max_pips` の隣)。
- **設定値検証 (R1 Medium#5)**: `OrchestratorEntryConfig.__post_init__` で `min_rr` が有限かつ > 0 であることを検証し、違反は起動時 ValueError (hard gate を config で実質無効化 (min_rr=0 / NaN) できる穴を塞ぐ)。上限は設けない (過大 min_rr は全 reject = fail-visible な誤設定で危険側でないため — YAGNI)。
- **gate 単一構築 (R2 High#2)**: 現行は gate が二重構築されている — `_build_planning_pipeline` (bootstrap.py:455) が pipeline 用を作り、runtime は `risk_gate` 未注入のためコンストラクタ fallback (`runtime.py:172`) で別インスタンスを作る。この構造では config を pipeline 側に繋いでも **live final gate は既定 1.5 のまま** (例: min_rr=2.5 設定時、planning は 2.5 で切るが RR 1.8 の plan が過去に存在すれば live gate は通す)。対処: `build_orchestrator_runtime` で `RiskGateWorker(min_rr=..., spread_max_pips=...)` を **1 個だけ**構築し、`_build_planning_pipeline` へ引数で渡し、`OrchestratorRuntime(risk_gate=...)` にも注入する。runtime の fallback 構築 (`risk_gate or RiskGateWorker(...)`) はテスト用に残すが、本番経路は常に注入。
- `settings.yaml.example` の orchestrator.entry に追記:

```yaml
    # 最低 reward/risk 比 (決定的導出 RR で判定)。プロンプトは RR >= 2 を狙わせ、
    # gate は 1.5 で切る (マージン構造は意図的)。
    min_rr: 1.5
```

- 既定 1.5 = 現行ハードコードと同値 → config 未記載の既存環境で gate 閾値は不変。gate 共有化も既定値では挙動不変 (両インスタンスとも 1.5 だったものが 1 インスタンス 1.5 になるだけ)。

---

## 3. 変更しないもの (スコープ外)

- `TradingConfig.min_rr_ratio` (旧 cycle 用) — 別系統のまま。旧 cycle 経路の物理削除 (Phase 3-3) で整理。
- `QuoteSnapshot` (context_builder) への有限値検証追加 — quote の NaN は `derive_rr` の防御 (非有限候補 → rr=0.0 reject) で偽 pass を塞ぐ。provider 層の検証は別課題。
- SL/TP side チェックの entry 基準統一 (現状 mid 基準) — RR は最悪ケース entry で導出するが、side チェックは mid のままとする。指値が mid を跨ぐ draft は稀で、跨ぐ場合も RR 導出が先に異常値を出す。過剰な同時変更を避ける。
- `expires_at` 等ほかの schema 検証の寛容化 — parse 救済 (2.D) はエラー種を選ばず SchemaParseError 全体を feedback 化するので個別対応不要。
- claude-cli 429 対策 — 別課題 (ロードマップ §2 信頼性課題)。
- 稼働環境の reject 分布再計測 — 動作確認完了後に別途。

---

## 4. 挙動変化まとめ

| ケース | 現行 | 変更後 |
|---|---|---|
| rr 過大申告・実 RR < 1.5 | **偽 pass** (plan 作成) | fixable reject → redraft 1 回 |
| rr 過小申告・実 RR >= 1.5 | fixable reject | pass (coerce で plan には導出値) |
| rr 申告なし | fixable reject (`missing rr`) | 導出できれば導出値で判定 (欠落だけでは reject しない) |
| rr 申告と導出の乖離 | 申告値のまま plan 保存 | 導出可能なら常に導出値で上書き。乖離 > 10% は INFO ログ + 不一致が SQL 測定可能 |
| entry 候補ゼロ (price 条件なし & quote なし) | 申告 rr で判定 | fixable reject (`rr underivable`) |
| **押し目 plan** (entry 条件が現在価格の有利側、計画 RR >= min) | 申告 rr 次第 | **pass** (planning は計画 RR で判定 — 実勢との距離では reject しない) |
| **live trigger 時のオーバーシュート** (breakout 後に実行価格が TP 側へ走り実行 RR 劣化) | **偽 pass** (計画 RR のまま発注) | live final gate (`include_executable_price=True`) で実行価格候補が min を引き下げ fixable reject → abandoned/invalidated → replan |
| **min_rr を config で変更** | 不可 (planning/live とも 1.5 固定) | planning・live final gate・shadow precheck の**全てに同一値** (gate 単一構築) |
| action の sl/tp/rr が str 数値 | dict のまま素通り (gate で潜在 TypeError) | float へ正規化 |
| action の sl/tp/rr が NaN/Inf/bool | 素通り (算術で汚染) | SchemaParseError → redraft 救済 |
| entry 条件の value が NaN/Inf/bool | `_opt_float` が素通し → gate 偽通過リスク | SchemaParseError → redraft 救済 |
| quote の bid/ask/mid が NaN | NaN 比較 False で偽 pass | derive_rr が rr=0.0 扱い → reject |
| draft parse 失敗 (未知 invalidation type 等) | run 全体 failed・救済なし・**監査痕跡なし** | WARNING ログ + redraft 1 回 → 再失敗なら failed (互換) |
| scan / final の parse 失敗 | failed | failed (不変) |
| min_rr | 1.5 固定 | config で調整可 (既定 1.5、有限・正数検証) |

---

## 5. テスト方針 (TDD)

### schemas (数値正規化 — 2.A′)

- action: str 数値 (`"149.0"`) → float 変換 / NaN / Infinity / bool / 非数値 str → SchemaParseError
- sl/tp/rr 欠落は正規化では通す (gate の責務)
- entry/invalidation 条件の value / value_pips: NaN / Inf / bool → SchemaParseError (`_opt_float` 強化、R2 High#3)
- draft 復元経路 (`_build_execution_draft` 相当のコンストラクタ直呼び) でも正規化が効く

### risk_gate (derive_rr + gate 判定)

- **phase 分離 (R2 High#1)**: 押し目 plan (long, ask=151, entry 条件=149.5, SL=148.5, TP=151.5) → `include_executable_price=False` で pass (計画 RR 2.0) / `True` で reject (実行 RR 0.2)
- price 条件複数 → 候補ごとの RR の min (R1 High#1 の例: long SL=149/TP=152、entry候補 150 (RR 2.0) と 151 (RR 0.5) → 0.5 を採用)
- 退化候補 (risk <= 0 / reward < 0) → rr 0.0 として min に参加 → reject
- **非有限候補** (quote の ask=NaN 等) → rr 0.0 → reject (NaN 比較 False の偽 pass をしない、R2 High#3)
- bid/ask 欠落 → mid フォールバック
- price 条件なし → include_executable_price=False でも実行価格 fallback で導出
- 候補ゼロ (price 条件なし & quote なし) / sl 欠落 → None
- derived < min_rr → fixable issue (メッセージに derived と claimed 併記)
- derived >= min_rr かつ申告 rr < min_rr → **pass** (申告を見ない回帰確認)
- rr underivable → fixable issue
- **live オーバーシュート再現** (R1 High#2 の例: long breakout=150 / SL=149 / TP=152 / trigger 時 ask=151.8 → 実行 RR ≈ 0.07 → `include_executable_price=True` で reject)

### live final gate 統合 (R2 Medium#4)

`test_taskf_execute_live_trigger` の枠組み (`make_live_runtime` に **実 RiskGateWorker** を注入) で:

- breakout オーバーシュート plan の trigger → **broker 不呼出** + intent=`abandoned` + plan=`invalidated`
- 押し目 plan (trigger 時実行価格が条件値の有利側) → 発注される (live で誤 reject しない回帰)
- bootstrap wiring: config の min_rr が pipeline と runtime に**同一 gate インスタンス**で届く

### config 検証

- min_rr = 0 / 負 / NaN → 起動時 ValueError

### planning_pipeline (coerce + parse 救済)

- 導出可能 → action.rr が常に導出値に置換・**agent_outputs には申告値が残る** (record_agent_output の呼び出し引数で検証)
- 乖離 > 10% / 申告 None → INFO ログ発火、乖離 <= 10% → ログなし (置換はどちらも実施)
- 導出不能 (quote なし等) → coerce せず gate reject に委ねる
- draft 1 回目 SchemaParseError → WARNING ログ (pair/attempt/例外) + feedback 付き redraft → 2 回目成功 → plan_create (redraft_count=1)
- draft 2 回連続 SchemaParseError → failed (エラーメッセージ互換)
- parse 救済で redraft 予算消費後の fixable reject → 再起案せず reject 終端
- scan / final の SchemaParseError → 従来通り failed (救済されない)

### config

- `OrchestratorEntryConfig.min_rr` 既定 1.5 / yaml 指定値のロード
- bootstrap が gate に min_rr を渡す (構築引数の検証)

---

## 6. 実装順 (plan 化時の粒度目安)

1. action 数値正規化 (2.A′, schemas.py) — derive_rr の入力保証を先に固める
2. `derive_rr` 純関数 + 単体テスト (risk_gate.py)
3. gate の RR 判定差し替え (2.B) — live オーバーシュートケースの検証を含む
4. config 化 (2.E)
5. pipeline coerce (2.C)
6. pipeline parse 救済 + 監査ログ (2.D)
7. 統合テスト (パイプライン end-to-end での挙動変化表 §4 の検証)
