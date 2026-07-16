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
| D-1 | RR 導出の entry 基準 | **entry 候補 = price 系 entry_condition の value 全部 + 実行価格 (executable price: long=ask / short=bid、無ければ mid)。候補ごとに RR を計算し最小値を採用** (レビュー High#1/#2 反映 — 詳細は §2.A)。planning gate と live final gate は同一実装 (`pre_check` 共有) でこの保守則が両方に効く |
| D-2 | 申告 rr の扱い | **導出可能なら常に導出値で置換** (レビュー Medium#3 反映)。申告値は agent_outputs に残し、plan には導出値を保存。乖離 10% 超は INFO ログ (不一致メトリクスの閾値としてのみ使用) |
| D-3 | スコープ | **SchemaParseError 救済も本 spec に含める** (同じ draft ループの修正、redraft 予算 max_redraft=1 を共有) |
| D-4 | min_rr | **config 化して接続** (既定 1.5 = 現行同値、挙動互換)。有限・正数の設定値検証付き (レビュー Medium#5) |
| D-5 | action の数値検証 | **schema 構築時に sl/tp/rr を有限 float へ正規化** (str 数値は変換、bool/NaN/Inf は拒否 → SchemaParseError → 2.D の redraft 救済に乗る) (レビュー Medium#4) |

---

## 2. 変更内容

### 2.A RR 導出関数 (risk_gate.py)

```python
def derive_rr(draft: ExecutionPlanDraft, quote: dict | None) -> float | None:
    """sl/tp/entry 候補から reward/risk 比を決定的に導出する。導出不能なら None。

    entry 候補 (全部を評価する):
      - price 系 entry_condition (price_at_or_below / price_at_or_above /
        breakout_above / breakout_below) の value 全部
      - 実行価格 (executable price): long → quote.ask / short → quote.bid、
        無ければ quote.mid (それも無ければ実行価格候補なし)
    候補ごとに rr = reward / risk を計算し、**最小値を採用** (保守則):
      long : reward = tp - entry, risk = entry - sl
      short: reward = entry - tp, risk = sl - entry
    退化候補 (risk <= 0: entry が SL の防御側に無い / reward < 0: entry が TP を
    超えている) は「その entry では成立しない」= rr 0 相当なので **0.0 として
    min に参加させる** (黙って除外すると悪い候補ほど無視される)。
    None 条件: 候補ゼロ (price 条件なし & 実行価格なし) / sl or tp 欠落。
    """
```

- **最小値採用の理由 (レビュー High#1)**: long で entry が SL に近いほど risk 分母が小さく RR は**大きく**なる。「SL に最も近い entry = 最悪」は逆。最悪ケースは候補ごとに RR を出した上での min でしか正しく取れない。
- **実行価格を候補に含める理由 (レビュー High#2)**: live final gate (`runtime.py` `_execute_live_trigger` → `pre_check(draft, trigger_ctx)`) は保存条件から復元した draft を trigger 時 context で再検証する。price 条件値だけを entry にすると、breakout オーバーシュート (例: breakout=150 / SL=149 / TP=152 で trigger 時 ask=151.8 → 実行 RR ≈ 0.08) が計画 RR 2.0 のまま通過する。実行価格候補を常に min に含めることで、**planning gate = 計画 RR / live gate = min(計画 RR, 実行 RR)** が単一実装で成立する (planning 時は quote が条件値より有利側にあるのが通常で、min は自然に条件値 RR になる)。約定想定は buy=ask / sell=bid (spread 込み)、bid/ask 欠落 provider では mid フォールバック。
- 純関数。module-level に置き pipeline (coerce) からも import する — 二重実装しない。
- `_ENTRY_PRICE_TYPES` (schemas.py の price 系 4 種) を判定に使う。`spread_below` / `technical_status_is` は entry 価格を持たないので無視。

### 2.A′ action の数値正規化 (schemas.py) — レビュー Medium#4

`ExecutionPlanDraft.from_llm_json` の `action` 受け入れ時に sl / tp / rr を正規化する:

- 値が存在する場合: `float()` へ変換し、**有限であること** (`math.isfinite`) を検証。bool は数値として拒否 (`isinstance(v, bool)` を先に弾く)。str 数値 (`"149.0"`) は変換を許容 (ローカル LLM の揺れ対策)。
- 変換不能 / NaN / Infinity → `ValueError` → 既存の except で `SchemaParseError` 化 → **2.D の redraft 救済に自然に乗る** (feedback にエラー内容が入る)。
- sl / tp / rr の**欠落は正規化では拒否しない** (欠落の扱いは gate の責務: `missing sl/tp` issue)。
- action 内の他キー (size_policy / comment 等) は従来通り未検証。
- `_build_execution_draft` (runtime の draft 復元) はコンストラクタ直呼びなので、正規化は `from_llm_json` と `__post_init__` のどちらに置くかを実装時に選ぶ — 保存済み plan (旧データ) に str 数値が残っている可能性を考えると `__post_init__` での正規化が復元経路もカバーして安全。ただし frozen でない dataclass の action dict 差し替えで足りる。

### 2.B gate の RR チェックを導出ベースに変更 (risk_gate.py)

`_fixable_issues` の RR 節を差し替え:

- `derived = derive_rr(draft, context.get("quote"))`
- `derived is None` → issue `"rr underivable (no entry candidate)"` を fixable に追加 (sl/tp 欠落時は既存の `missing sl` / `missing tp` issue が先に立つので、この issue は entry 起因のケースを主に拾う)。**楽観通過させない** — spread unknown を fixable reject にしたのと同じ思想。
- `derived < min_rr` → issue `f"derived rr {derived:.2f} below min {min_rr} (claimed {claimed})"`。claimed (申告値、None なら "none") を併記し redraft feedback を具体化する。退化候補 (rr=0.0) が min を引き下げたケースもこの分岐で reject される。
- 申告 `action["rr"]` は gate では比較に使わない。`missing rr` issue は廃止 (申告は任意の参考値になる)。
- **live final gate への効果 (レビュー High#2)**: `pre_check` は runtime の live final gate からも呼ばれるため、この差し替えだけで trigger 時実行価格による実行 RR 検証が live 経路にも効く。runtime 側のコード変更は不要。live gate での RR reject は fixable 分類 → intent は `abandoned`・plan は `invalidated` (terminal)・再発注は replan 経由、という既存の遷移に乗る (runtime.py の分岐は不変)。

### 2.C 申告 rr の coerce (planning_pipeline.py)

scale_in coerce (`draft.scale_in != same_dir` ブロック) の直後、`final_decision` 呼び出しの前に追加:

- `derived = derive_rr(draft, context.get("quote"))`
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
- **設定値検証 (レビュー Medium#5)**: `OrchestratorEntryConfig.__post_init__` で `min_rr` が有限かつ > 0 であることを検証し、違反は起動時 ValueError (hard gate を config で実質無効化 (min_rr=0 / NaN) できる穴を塞ぐ)。上限は設けない (過大 min_rr は全 reject = fail-visible な誤設定で危険側でないため — YAGNI)。
- `bootstrap.py` の gate 構築を `RiskGateWorker(spread_max_pips=..., min_rr=config.orchestrator.entry.min_rr)` に変更。
- `settings.yaml.example` の orchestrator.entry に追記:

```yaml
    # 最低 reward/risk 比 (決定的導出 RR で判定)。プロンプトは RR >= 2 を狙わせ、
    # gate は 1.5 で切る (マージン構造は意図的)。
    min_rr: 1.5
```

- 既定 1.5 = 現行ハードコードと同値 → config 未記載の既存環境で gate 閾値は不変。

---

## 3. 変更しないもの (スコープ外)

- `TradingConfig.min_rr_ratio` (旧 cycle 用) — 別系統のまま。旧 cycle 経路の物理削除 (Phase 3-3) で整理。
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
| **live trigger 時のオーバーシュート** (breakout 後に実行価格が TP 側へ走り実行 RR 劣化) | **偽 pass** (計画 RR のまま発注) | live final gate で実行価格候補が min を引き下げ fixable reject → abandoned/invalidated → replan |
| action の sl/tp/rr が str 数値 | dict のまま素通り (gate で潜在 TypeError) | float へ正規化 |
| action の sl/tp/rr が NaN/Inf/bool | 素通り (算術で汚染) | SchemaParseError → redraft 救済 |
| draft parse 失敗 (未知 invalidation type 等) | run 全体 failed・救済なし・**監査痕跡なし** | WARNING ログ + redraft 1 回 → 再失敗なら failed (互換) |
| scan / final の parse 失敗 | failed | failed (不変) |
| min_rr | 1.5 固定 | config で調整可 (既定 1.5、有限・正数検証) |

---

## 5. テスト方針 (TDD)

### schemas (action 正規化 — 2.A′)

- str 数値 (`"149.0"`) → float 変換
- NaN / Infinity / bool / 非数値 str → SchemaParseError
- sl/tp/rr 欠落は正規化では通す (gate の責務)
- draft 復元経路 (`_build_execution_draft` 相当のコンストラクタ直呼び) でも正規化が効く

### risk_gate (derive_rr + gate 判定)

- price 条件 1 件 + 実行価格 (long=ask / short=bid) の 2 候補 → **min を採用** (long/short 両方向)
- price 条件複数 → 候補ごとの RR の min (レビュー High#1 の例: long SL=149/TP=152、entry候補 150 (RR 2.0) と 151 (RR 0.5) → 0.5 を採用)
- 退化候補 (risk <= 0 / reward < 0) → rr 0.0 として min に参加 → reject
- bid/ask 欠落 → mid フォールバック
- price 条件なし → 実行価格のみで導出
- 候補ゼロ (price 条件なし & quote なし) / sl 欠落 → None
- derived < min_rr → fixable issue (メッセージに derived と claimed 併記)
- derived >= min_rr かつ申告 rr < min_rr → **pass** (申告を見ない回帰確認)
- rr underivable → fixable issue
- **live オーバーシュート再現** (レビュー High#2 の例: long breakout=150 / SL=149 / TP=152 / trigger 時 ask=151.8 → 実行 RR ≈ 0.07 → reject)

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
