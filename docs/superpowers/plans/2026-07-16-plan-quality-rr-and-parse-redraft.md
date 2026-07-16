# plan 品質バグ修正 (RR 決定的検算 + draft parse 救済) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** risk gate の RR 判定を LLM 申告値から決定的導出値 (entry 候補 min) に置き換え、live final gate に実行価格 RR を効かせ、draft parse 失敗を redraft 1 回で救済する。

**Architecture:** spec `docs/superpowers/specs/2026-07-16-plan-quality-rr-and-parse-redraft-design.md` の §2 (2.A′ → 2.A → 2.B → 2.E → 2.C → 2.D)。`derive_rr` は risk_gate.py の module-level 純関数とし、gate と pipeline coerce で共有する。planning gate と live final gate は同一 `pre_check` のため gate 変更だけで両経路に効く (runtime 変更なし)。

**Tech Stack:** Python 3.11+ / dataclass schema / pytest (asyncio)。テストは既存の per-file 実行が回帰判定 ([[finance_fullsuite_order_flake]]: フル suite は順序依存フレークあり)。

**Base branch:** `feat/technical-llm-omit` (spec コミット済 HEAD の上に積む)。

---

## 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/orchestrator/schemas.py` | `ExecutionPlanDraft.__post_init__` に action の sl/tp/rr 数値正規化 (2.A′) |
| `src/orchestrator/risk_gate.py` | `derive_rr` 追加 + `_fixable_issues` の RR 節差し替え (2.A/2.B) |
| `src/config/schema.py` | `OrchestratorEntryConfig.min_rr` + `__post_init__` 検証 (2.E) |
| `src/orchestrator/bootstrap.py` | gate 構築に `min_rr` 接続 (2.E) |
| `config/settings.yaml.example` | `orchestrator.entry.min_rr` 追記 (2.E) |
| `src/orchestrator/planning_pipeline.py` | rr coerce (2.C) + draft parse 救済 (2.D) |
| `tests/test_orchestrator_schemas.py` | 2.A′ テスト追加 |
| `tests/test_risk_gate_worker.py` | derive_rr / gate 判定テスト追加・`missing rr` テスト更新 |
| `tests/test_orchestrator_config.py` | min_rr 設定テスト追加 |
| `tests/test_orchestrator_bootstrap.py` | bootstrap 接続テスト追加 |
| `tests/test_planning_pipeline.py` | coerce / parse 救済テスト追加 |

---

### Task 1: action 数値正規化 (2.A′, schemas.py)

**Files:**
- Modify: `src/orchestrator/schemas.py` (`ExecutionPlanDraft.__post_init__`, 現在 L247 付近)
- Test: `tests/test_orchestrator_schemas.py`

**背景:** `action` は未検証 dict。str 数値 (`"149.0"`)、NaN、Infinity、bool が入ると後続の `derive_rr` / 乖離計算が壊れる。`__post_init__` に置くのは runtime の draft 復元 (`_build_execution_draft` のコンストラクタ直呼び) もカバーするため。frozen でない dataclass なので `self.action` の差し替えは可能。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_orchestrator_schemas.py` の `TestExecutionPlanDraft` クラス (L241 付近) に追加。既存の `_valid_kwargs()` ヘルパを使う (存在は確認済み — L242 で使用中):

```python
    # ── action sl/tp/rr 正規化 (2026-07-16 spec 2.A′) ──────────

    def test_action_string_numbers_normalized_to_float(self) -> None:
        kw = self._valid_kwargs()
        kw["action"] = {"sl": "149.0", "tp": "152.0", "rr": "2.0", "comment": "x"}
        draft = ExecutionPlanDraft(**kw)
        assert draft.action["sl"] == 149.0
        assert isinstance(draft.action["sl"], float)
        assert draft.action["tp"] == 152.0
        assert draft.action["rr"] == 2.0

    def test_action_nan_rejected(self) -> None:
        kw = self._valid_kwargs()
        kw["action"] = {"sl": float("nan"), "tp": 152.0, "rr": 2.0}
        with pytest.raises(ValueError, match="sl"):
            ExecutionPlanDraft(**kw)

    def test_action_infinity_rejected(self) -> None:
        kw = self._valid_kwargs()
        kw["action"] = {"sl": 149.0, "tp": float("inf"), "rr": 2.0}
        with pytest.raises(ValueError, match="tp"):
            ExecutionPlanDraft(**kw)

    def test_action_bool_rejected(self) -> None:
        kw = self._valid_kwargs()
        kw["action"] = {"sl": True, "tp": 152.0, "rr": 2.0}
        with pytest.raises(ValueError, match="sl"):
            ExecutionPlanDraft(**kw)

    def test_action_non_numeric_string_rejected(self) -> None:
        kw = self._valid_kwargs()
        kw["action"] = {"sl": "around 149", "tp": 152.0, "rr": 2.0}
        with pytest.raises(ValueError, match="sl"):
            ExecutionPlanDraft(**kw)

    def test_action_missing_keys_allowed(self) -> None:
        # 欠落は正規化では拒否しない (gate の missing sl/tp issue の責務)。
        kw = self._valid_kwargs()
        kw["action"] = {"size_policy": "risk", "comment": "x"}
        draft = ExecutionPlanDraft(**kw)
        assert "sl" not in draft.action

    def test_action_none_values_allowed(self) -> None:
        # None は「欠落」と同義に扱い、そのまま通す。
        kw = self._valid_kwargs()
        kw["action"] = {"sl": None, "tp": 152.0, "rr": None}
        draft = ExecutionPlanDraft(**kw)
        assert draft.action["sl"] is None
        assert draft.action["tp"] == 152.0

    def test_from_llm_json_action_string_numbers_normalized(self) -> None:
        # from_llm_json 経由でも __post_init__ の正規化が効く。
        raw = json.dumps({
            "direction": "long",
            "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
            "action": {"sl": "149.0", "tp": "152.0", "rr": "2.0", "comment": "x"},
            "invalidation": [{"type": "expired"}],
            "expires_at": "2026-12-31T18:00:00+00:00",
            "reasoning_summary": "r",
        })
        draft = ExecutionPlanDraft.from_llm_json(raw)
        assert draft.action["sl"] == 149.0

    def test_from_llm_json_action_nan_raises_schema_parse_error(self) -> None:
        # JSON 標準に NaN は無いが json.loads は NaN/Infinity を受理する (Python 拡張)。
        raw = (
            '{"direction": "long",'
            '"entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],'
            '"action": {"sl": NaN, "tp": 152.0, "rr": 2.0},'
            '"invalidation": [{"type": "expired"}],'
            '"expires_at": "2026-12-31T18:00:00+00:00",'
            '"reasoning_summary": "r"}'
        )
        with pytest.raises(SchemaParseError):
            ExecutionPlanDraft.from_llm_json(raw)
```

ファイル冒頭の import に `json` が無ければ追加 (`import json`)。`SchemaParseError` / `pytest` は既存 import 済のはず — 無ければ追加。

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_orchestrator_schemas.py -k "action" -v`
Expected: 上記の reject 系テストが FAIL (現状は未検証で通ってしまうため `DID NOT RAISE`)。normalize 系も FAIL (str のまま)。

- [ ] **Step 3: 実装**

`src/orchestrator/schemas.py` — module 冒頭に `import math` を追加し、`ExecutionPlanDraft.__post_init__` (L247) を修正:

```python
    def __post_init__(self) -> None:
        # scale_in × new_signal_evidence の cross-field 検証は schema では行わない:
        # (既存コメントそのまま)
        if self.direction not in _DRAFT_DIRECTION:
            raise ValueError(f"direction must be long/short, got {self.direction!r}")
        if not self.entry_conditions:
            raise ValueError("entry_conditions must not be empty")
        # action の sl/tp/rr を有限 float へ正規化 (spec 2026-07-16 §2.A′)。
        # str 数値 ("149.0") はローカル LLM の揺れとして変換を許容。bool / NaN /
        # Infinity / 非数値は拒否 → from_llm_json 経由では SchemaParseError となり
        # redraft 救済 (§2.D) に乗る。欠落・None は gate の責務なので通す。
        # __post_init__ に置くのは runtime の draft 復元 (コンストラクタ直呼び)
        # もカバーするため。
        self.action = _normalize_action_numbers(self.action)
```

`ExecutionPlanDraft` クラス定義の直前 (module-level、`_DRAFT_DIRECTION` の下) にヘルパを追加:

```python
def _normalize_action_numbers(action: dict[str, Any]) -> dict[str, Any]:
    """action の sl/tp/rr を有限 float へ正規化した新 dict を返す。

    - str 数値 → float 変換 (ローカル LLM の揺れ対策)
    - bool / NaN / Infinity / 変換不能 → ValueError (呼び出し元で SchemaParseError 化)
    - 欠落・None → そのまま (gate の missing sl/tp issue の責務)
    """
    out = dict(action)
    for key in ("sl", "tp", "rr"):
        if key not in out or out[key] is None:
            continue
        v = out[key]
        if isinstance(v, bool):
            raise ValueError(f"action {key} must be numeric, got bool {v!r}")
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"action {key} must be numeric, got {v!r}") from exc
        if not math.isfinite(f):
            raise ValueError(f"action {key} must be finite, got {f!r}")
        out[key] = f
    return out
```

注意: `from_llm_json` は既に `except (KeyError, TypeError, ValueError)` で `SchemaParseError` に包む (L285) ので、`__post_init__` の ValueError は自動的に SchemaParseError 化される。追加変更不要。

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_orchestrator_schemas.py -v`
Expected: 全 PASS (既存テスト含む)。

- [ ] **Step 5: 影響を受ける既存テストの確認**

Run: `uv run pytest tests/test_risk_gate_worker.py tests/test_planning_pipeline.py tests/test_orchestrator_runtime.py tests/test_plan_ttl_clamp.py tests/test_planner_agent.py tests/test_planner_user_notes.py -q`
Expected: 全 PASS (既存 fixture の action は数値リテラルなので正規化は no-op)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/schemas.py tests/test_orchestrator_schemas.py
git commit -m "feat(orchestrator): normalize action sl/tp/rr to finite floats in ExecutionPlanDraft"
```

---

### Task 2: derive_rr 純関数 (2.A, risk_gate.py)

**Files:**
- Modify: `src/orchestrator/risk_gate.py`
- Test: `tests/test_risk_gate_worker.py`

**背景:** entry 候補 = price 系 entry_condition の value 全部 + 実行価格 (long=ask / short=bid、無ければ mid)。候補ごとに RR を計算し min を採用。退化候補 (risk<=0 / reward<0) は rr=0.0 として min に参加。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_risk_gate_worker.py` に追加。import に `derive_rr` を足す:

```python
from src.orchestrator.risk_gate import RiskGateResult, RiskGateWorker, derive_rr
```

テストクラスを追加 (`_draft` ヘルパは既存 L19 のものを使う — entry は `price_at_or_below value=150.0`):

```python
def _draft_entries(entries, *, direction="long", sl=149.0, tp=152.0) -> ExecutionPlanDraft:
    """entry_conditions を差し替えた draft を作る (derive_rr テスト用)。"""
    return ExecutionPlanDraft(
        direction=direction,
        entry_conditions=entries,
        action={"sl": sl, "tp": tp, "size_policy": "risk", "rr": 2.0, "comment": "x"},
        invalidation=[InvalidationCondition(type="price_below", value=148.0)],
        expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
        reasoning_summary="r",
    )


class TestDeriveRr:
    """derive_rr (spec 2026-07-16 §2.A): entry 候補 min の決定的 RR 導出。"""

    QUOTE = {"bid": 149.99, "ask": 150.01, "mid": 150.0, "spread": 0.02}

    def test_long_single_price_condition_with_quote(self) -> None:
        # 候補: 条件値150 (rr=(152-150)/(150-149)=2.0)、ask=150.01 (rr≈1.97)。min≈1.97
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        rr = derive_rr(draft, self.QUOTE)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_short_uses_bid_as_executable(self) -> None:
        # short: reward=entry-tp, risk=sl-entry。実行価格は bid。
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_above", value=150.0)],
            direction="short", sl=151.0, tp=148.0,
        )
        rr = derive_rr(draft, self.QUOTE)
        # 候補: 150 → (150-148)/(151-150)=2.0 / bid=149.99 → (1.99)/(1.01)≈1.970
        assert rr == pytest.approx((149.99 - 148.0) / (151.0 - 149.99))

    def test_multiple_price_conditions_takes_min(self) -> None:
        # レビュー High#1 の例: long SL=149/TP=152、候補 150 (rr 2.0) と 151 (rr 0.5)
        # → 0.5 を採用 (SL に近い 150 は最良側)。quote なしで条件値のみ評価。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=151.0),
        ])
        rr = derive_rr(draft, None)
        assert rr == pytest.approx(0.5)

    def test_live_overshoot_executable_price_dominates(self) -> None:
        # レビュー High#2 の例: breakout=150, SL=149, TP=152, trigger 時 ask=151.8
        # → 実行 RR=(152-151.8)/(151.8-149)≈0.071 が min。
        draft = _draft_entries([EntryCondition(type="breakout_above", value=150.0)])
        quote = {"bid": 151.78, "ask": 151.8, "mid": 151.79, "spread": 0.02}
        rr = derive_rr(draft, quote)
        assert rr == pytest.approx((152.0 - 151.8) / (151.8 - 149.0))

    def test_degenerate_candidate_counts_as_zero(self) -> None:
        # entry が TP を超えている候補 (reward<0) は rr=0.0 として min に参加。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=152.5),  # > TP
        ])
        assert derive_rr(draft, None) == 0.0

    def test_risk_nonpositive_candidate_counts_as_zero(self) -> None:
        # entry が SL の防御側に無い候補 (long で entry <= sl) は rr=0.0。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=148.5),  # < SL
        ])
        assert derive_rr(draft, None) == 0.0

    def test_no_price_condition_uses_executable_only(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, self.QUOTE)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_missing_bid_ask_falls_back_to_mid(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, {"mid": 150.0, "spread": None})
        assert rr == pytest.approx(2.0)

    def test_no_candidates_returns_none(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        assert derive_rr(draft, None) is None
        assert derive_rr(draft, {}) is None

    def test_missing_sl_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], sl=None,
        )
        assert derive_rr(draft, self.QUOTE) is None

    def test_missing_tp_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], tp=None,
        )
        assert derive_rr(draft, self.QUOTE) is None
```

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py::TestDeriveRr -v`
Expected: FAIL (`ImportError: cannot import name 'derive_rr'`)

- [ ] **Step 3: 実装**

`src/orchestrator/risk_gate.py` — import に追加:

```python
from src.orchestrator.schemas import ExecutionPlanDraft, _ENTRY_PRICE_TYPES
```

(`_ENTRY_PRICE_TYPES` は schemas.py L86 の module-level frozenset。private 名だが同 package 内共有とする — 二重定義でズレるより安全。)

`_default_pip_size_for` の下 (module-level) に追加:

```python
def _executable_price(direction: str, quote: dict[str, Any] | None) -> float | None:
    """約定想定価格: long は ask (買い)、short は bid (売り)。無ければ mid。"""
    if not quote:
        return None
    price = quote.get("ask") if direction == "long" else quote.get("bid")
    if price is None:
        price = quote.get("mid")
    return price


def derive_rr(draft: ExecutionPlanDraft, quote: dict[str, Any] | None) -> float | None:
    """sl/tp/entry 候補から reward/risk 比を決定的に導出する (spec 2026-07-16 §2.A)。

    entry 候補 = price 系 entry_condition の value 全部 + 実行価格
    (long=ask / short=bid、無ければ mid)。候補ごとに rr を計算し **最小値** を
    採用する (保守則 — SL に近い entry ほど rr は大きく出るため、min でしか
    最悪ケースを取れない)。退化候補 (risk<=0 / reward<0) は「その entry では
    成立しない」= rr 0.0 として min に参加させる (黙って除外すると悪い候補
    ほど無視される)。

    None (導出不能): sl or tp 欠落 / entry 候補ゼロ。
    live final gate は同じ pre_check を trigger 時 context で呼ぶため、実行価格
    候補が trigger 時のオーバーシュートを自動的に検出する (レビュー High#2)。
    """
    sl = draft.action.get("sl")
    tp = draft.action.get("tp")
    if sl is None or tp is None:
        return None

    candidates: list[float] = [
        c.value for c in draft.entry_conditions
        if c.type in _ENTRY_PRICE_TYPES and c.value is not None
    ]
    executable = _executable_price(draft.direction, quote)
    if executable is not None:
        candidates.append(executable)
    if not candidates:
        return None

    def _rr(entry: float) -> float:
        if draft.direction == "long":
            reward, risk = tp - entry, entry - sl
        else:
            reward, risk = entry - tp, sl - entry
        if risk <= 0 or reward < 0:
            return 0.0
        return reward / risk

    return min(_rr(e) for e in candidates)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py::TestDeriveRr -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/risk_gate.py tests/test_risk_gate_worker.py
git commit -m "feat(orchestrator): add derive_rr — deterministic min-over-candidates RR derivation"
```

---

### Task 3: gate の RR 判定差し替え (2.B, risk_gate.py)

**Files:**
- Modify: `src/orchestrator/risk_gate.py` (`_fixable_issues`, L147-152 付近)
- Test: `tests/test_risk_gate_worker.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_risk_gate_worker.py` に追加 (`worker` fixture は既存 L53: `min_rr=1.5`):

```python
class TestDerivedRrGate:
    """gate の RR 判定は申告値でなく derive_rr の導出値を使う (spec §2.B)。"""

    def test_overclaimed_rr_rejected_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=3.0 だが実 RR は (150.5-150.01)/(150.01-149.0)≈0.49 (tp=150.5)。
        # 旧実装 (申告比較) では pass していた偽 pass ケース。
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx())
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)

    def test_underclaimed_rr_passes_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=0.5 だが実 RR ≈ 1.97 (>= 1.5)。旧実装では誤 reject していたケース。
        d = _draft(rr=0.5)
        res = worker.pre_check(d, _ctx())
        assert res.passed is True, res.issues

    def test_missing_rr_claim_passes_when_derivable(self, worker: RiskGateWorker) -> None:
        # 申告 rr 欠落は reject 理由にしない (導出できるため)。missing rr issue は廃止。
        d = _draft(rr=None)
        res = worker.pre_check(d, _ctx())
        assert res.passed is True, res.issues

    def test_issue_message_includes_claimed(self, worker: RiskGateWorker) -> None:
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx())
        msg = next(i for i in res.issues if "derived rr" in i)
        assert "claimed 3.0" in msg

    def test_rr_underivable_is_fixable(self, worker: RiskGateWorker) -> None:
        # price 条件なし + quote なし → 候補ゼロ → underivable reject。
        # (sl/tp はあるので missing sl/tp issue は立たない)
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="spread_below", value_pips=2.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        ctx = _ctx()
        ctx["quote"] = {}  # bid/ask/mid なし (spread も無いが underivable を先に確認)
        res = worker.pre_check(d, ctx)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("underivable" in i for i in res.issues)

    def test_live_overshoot_rejected(self, worker: RiskGateWorker) -> None:
        # レビュー High#2: breakout=150/SL=149/TP=152、trigger 時 ask=151.8
        # → 実行 RR≈0.07 < 1.5 → live final gate 相当で reject。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="breakout_above", value=150.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        res = worker.pre_check(d, _ctx(mid=151.79))
        assert res.passed is False
        assert any("derived rr" in i for i in res.issues)
```

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py::TestDerivedRrGate -v`
Expected: FAIL (旧実装は申告 rr 比較のため overclaim が pass / underclaim が reject / missing rr が reject)

- [ ] **Step 3: 実装**

`src/orchestrator/risk_gate.py` の `_fixable_issues` — RR 節 (L147-152) を差し替え:

```python
        # RR: LLM 申告 (action["rr"]) は信用せず derive_rr の導出値で判定する
        # (spec 2026-07-16 §2.B — 申告過大の偽 pass / 申告過小の誤 reject を両方塞ぐ)。
        # 導出不能は楽観通過させず fixable reject (spread unknown と同じ思想)。
        derived = derive_rr(draft, context.get("quote"))
        if derived is None:
            if sl is not None and tp is not None:
                # sl/tp 欠落時は上の missing issue が既に立っている — entry 起因のみ追加。
                issues.append("rr underivable (no entry candidate)")
        elif derived < self._min_rr:
            claimed = action.get("rr")
            issues.append(
                f"derived rr {derived:.2f} below min {self._min_rr}"
                f" (claimed {claimed if claimed is not None else 'none'})"
            )
```

旧コード (削除対象):

```python
        # RR: 欠落も下限割れも fixable (再起案で直せる)。
        rr = action.get("rr")
        if rr is None:
            issues.append("missing rr")
        elif rr < self._min_rr:
            issues.append(f"rr {rr} below min {self._min_rr}")
```

- [ ] **Step 4: 既存テストの更新**

`test_missing_rr_is_fixable` (L186 付近) は仕様変更で無効。**削除**し、Step 1 の `test_missing_rr_claim_passes_when_derivable` が代替であることを確認。`test_rr_below_min_is_fixable` (L140) は申告 rr で下限割れを作っている可能性が高い — 実 RR も下限割れになるよう tp を近づける形に修正:

```python
    def test_rr_below_min_is_fixable(self, worker: RiskGateWorker) -> None:
        # 実 RR ≈ (150.5-150.01)/(150.01-149.0) ≈ 0.49 < 1.5
        res = worker.pre_check(_draft(tp=150.5), _ctx())
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)
```

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py -v`
Expected: 全 PASS。既存の `test_clean_long_passes` / `test_clean_short_passes` 等も PASS (fixture の RR は実 RR でも 1.5 以上)。short 系 fixture が bid ベースで下限を割らないか出力を確認 — 割る場合は tp/sl を調整し、調整理由をコメントに残す。

- [ ] **Step 6: runtime テストの回帰確認**

Run: `uv run pytest tests/test_orchestrator_runtime.py tests/test_orchestrator_e2e.py -q`
Expected: 全 PASS。live gate / shadow precheck 経路の fixture が新 RR 判定で reject に変わる場合は、fixture の sl/tp を実 RR >= 1.5 に調整する (テストの意図は RR でなく遷移検証のため)。

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/risk_gate.py tests/test_risk_gate_worker.py
git commit -m "feat(orchestrator): risk gate judges RR by derived value, not LLM claim"
```

---

### Task 4: min_rr の config 化 (2.E)

**Files:**
- Modify: `src/config/schema.py` (`OrchestratorEntryConfig`, L675-683)
- Modify: `src/orchestrator/bootstrap.py` (L455-457)
- Modify: `config/settings.yaml.example` (L422 の `entry:` ブロック)
- Test: `tests/test_orchestrator_config.py`, `tests/test_orchestrator_bootstrap.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_orchestrator_config.py` に追加 (既存 L25 の `"entry": {"spread_max_pips": 1.5}` パターンに倣う。ファイル内の既存 loader ヘルパの使い方を確認して同じ形式で):

```python
def test_entry_min_rr_default_and_override() -> None:
    from src.config.schema import OrchestratorEntryConfig

    assert OrchestratorEntryConfig().min_rr == 1.5
    assert OrchestratorEntryConfig(min_rr=2.0).min_rr == 2.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_entry_min_rr_invalid_rejected(bad) -> None:
    from src.config.schema import OrchestratorEntryConfig

    with pytest.raises(ValueError, match="min_rr"):
        OrchestratorEntryConfig(min_rr=bad)
```

`tests/test_orchestrator_bootstrap.py` に追加。既存の `test_planner_and_exec_get_separate_agent_llms` (L331) と同じパターン: `_build_client` を stub し `_patch_orch_store` → `bs.build_orchestrator_runtime` → `rt._pipeline` の private 属性を検証:

```python
def test_risk_gate_receives_min_rr_from_config(tmp_path: Path, monkeypatch) -> None:
    """bootstrap が orchestrator.entry.min_rr を RiskGateWorker に渡す (spec 2.E)。"""
    monkeypatch.setattr(
        "src.llm.factory._build_client",
        lambda provider, pc, model: ("client", provider, model),
    )
    _patch_orch_store(monkeypatch, tmp_path)

    cfg = _config(enabled=True, tmp_path=tmp_path)
    cfg.llm.provider = "claude-cli"
    cfg.orchestrator.entry.min_rr = 2.5

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt._pipeline._risk._min_rr == 2.5
```

(注: `_config` が返す AppConfig で `cfg.llm.price_analysis.model` 等の追加設定が必要なら L364 `test_no_agents_yaml_falls_back_to_price_analysis` の設定行をコピーする。)

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_orchestrator_config.py -k min_rr tests/test_orchestrator_bootstrap.py -k min_rr -v`
Expected: FAIL (`min_rr` フィールドが存在しない / gate に渡っていない)

- [ ] **Step 3: 実装**

`src/config/schema.py` L675-683 を修正:

```python
@dataclass
class OrchestratorEntryConfig:
    """entry 感度・gate 閾値 (spec §12)。"""
    price_move_pct: float = 0.15
    spread_max_pips: float = 2.0
    # 最低 reward/risk 比 (決定的導出 RR で判定, spec 2026-07-16)。プロンプトは
    # RR >= 2 を狙わせ、gate は 1.5 で切る (マージン構造は意図的)。
    min_rr: float = 1.5
    news_impact_min: float = 0.5
    require_fresh_technical: bool = True
    max_quote_age_seconds: int = 10
    max_technical_age_seconds: int = 1800

    def __post_init__(self) -> None:
        # min_rr <= 0 / NaN は hard gate を実質無効化するため起動時に拒否する
        # (レビュー Medium#5)。上限は設けない — 過大値は全 reject で fail-visible。
        v = self.min_rr
        if not isinstance(v, (int, float)) or isinstance(v, bool) or \
                not math.isfinite(v) or v <= 0:
            raise ValueError(f"orchestrator.entry.min_rr must be finite and > 0, got {v!r}")
```

module 冒頭に `import math` が無ければ追加。

`src/orchestrator/bootstrap.py` L455-457 を修正:

```python
        risk_gate=RiskGateWorker(
            min_rr=config.orchestrator.entry.min_rr,
            spread_max_pips=config.orchestrator.entry.spread_max_pips,
        ),
```

`config/settings.yaml.example` の `entry:` ブロック (L422) に追記:

```yaml
  entry:
    price_move_pct: 0.15
    spread_max_pips: 2.0
    # 最低 reward/risk 比 (決定的導出 RR で判定)。プロンプトは RR >= 2 を狙わせ、
    # gate は 1.5 で切る (マージン構造は意図的)。
    min_rr: 1.5
    news_impact_min: 0.5
    require_fresh_technical: true
    max_quote_age_seconds: 10
    max_technical_age_seconds: 5400  # 1800 → 5400 (staleness 90min と整合, day)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_orchestrator_config.py tests/test_orchestrator_bootstrap.py tests/test_config_example_sync.py tests/test_config_loader.py -q`
Expected: 全 PASS (`test_config_example_sync` は example の load 可能性を検証するため必ず回す)

- [ ] **Step 5: Commit**

```bash
git add src/config/schema.py src/orchestrator/bootstrap.py config/settings.yaml.example tests/test_orchestrator_config.py tests/test_orchestrator_bootstrap.py
git commit -m "feat(orchestrator): make risk gate min_rr configurable with validation"
```

---

### Task 5: 申告 rr の coerce (2.C, planning_pipeline.py)

**Files:**
- Modify: `src/orchestrator/planning_pipeline.py` (draft ループ内、scale_in coerce の直後 L239-247 付近)
- Test: `tests/test_planning_pipeline.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_planning_pipeline.py` に追加。既存の `_draft_json` / `_ctx` / `_make_pipeline` / `OPP_YES` / `FINAL_ACCEPT` を使う (全て確認済み)。`_ctx` の quote は `{"bid": mid-0.01, "ask": mid+0.01, "mid": mid, "spread": 0.02}` (mid=150.0 → ask=150.01)。draft の entry は `price_at_or_below 150.0`、sl=149.0、tp=152.0 → 導出 RR = min(条件値 rr=2.0, ask rr≈1.9702) ≈ 1.9702:

```python
# ── rr coerce (spec 2026-07-16 §2.C) ─────────────────────────


async def test_rr_claim_always_replaced_with_derived(store: OrchestratorStore) -> None:
    # 申告 rr=9.9 (乖離>10%) → plan の action.rr は導出値 (≈1.97) に置換。
    llm = _ScriptedLLM([OPP_YES, _draft_json(rr=9.9), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    active = store.get_active_plans("USDJPY=X")
    expected = round((152.0 - 150.01) / (150.01 - 149.0), 2)
    assert active[0].action_json["rr"] == expected


async def test_rr_claim_within_tolerance_still_replaced(store: OrchestratorStore) -> None:
    # 乖離 <=10% でも置換は無条件 (レビュー Medium#3)。ログ発火だけが閾値依存。
    llm = _ScriptedLLM([OPP_YES, _draft_json(rr=2.0), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    active = store.get_active_plans("USDJPY=X")
    expected = round((152.0 - 150.01) / (150.01 - 149.0), 2)
    assert active[0].action_json["rr"] == expected


async def test_rr_claim_preserved_in_agent_outputs(store: OrchestratorStore) -> None:
    # 申告値は agent_outputs (coerce 前に永続化) に残る — 不一致率の SQL 測定材料
    # (scale_in と同じ検証パターン: test_scale_in_coerced_false_without_position 参照)。
    llm = _ScriptedLLM([OPP_YES, _draft_json(rr=9.9), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    draft_outputs = [
        o for o in store.get_agent_outputs(run_id)
        if o.output_type == "execution_draft"
    ]
    assert len(draft_outputs) == 1
    assert draft_outputs[0].structured_payload_json["action"]["rr"] == 9.9
```

(注: active plan の取得は `store.get_active_plans(pair)`、agent_outputs の payload 属性は `structured_payload_json` — 既存テスト L429-439 で確認済みのパターン。`_draft_json(rr=...)` はデフォルト sl=149/tp=152/entry=150。)

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_planning_pipeline.py -k rr_claim -v`
Expected: FAIL (plan.action_json["rr"] が申告値のまま)

- [ ] **Step 3: 実装**

`src/orchestrator/planning_pipeline.py` — import に `derive_rr` を追加:

```python
from src.orchestrator.risk_gate import RiskGateWorker, derive_rr
```

(既存 import が `from src.orchestrator.risk_gate import RiskGateWorker` の形か確認し、同じ行に足す。)

scale_in coerce ブロック (L239-247 `if draft.scale_in != same_dir:` の直後)、`final = await self._planner.final_decision(...)` の前に追加:

```python
            # P-2c: rr の正本も決定的導出 (spec 2026-07-16 §2.C)。申告値は
            # _persist_opinion で agent_outputs に保存済み — plan には導出値を
            # 保存する (置換は無条件、レビュー Medium#3)。乖離 >10% のみログ
            # (不一致メトリクスの発火閾値)。導出不能時は coerce せず gate の
            # underivable reject に委ねる。
            derived_rr = derive_rr(draft, context.get("quote"))
            if derived_rr is not None:
                claimed_rr = draft.action.get("rr")
                if claimed_rr is None or abs(claimed_rr - derived_rr) > 0.10 * derived_rr:
                    logger.info(
                        "[ORCH] rr claim overridden for %s: llm=%s derived=%.2f",
                        pair, claimed_rr, derived_rr,
                    )
                new_action = dict(draft.action)
                new_action["rr"] = round(derived_rr, 2)
                draft = replace(draft, action=new_action)
```

(`replace` は既に L27 で import 済み: `from dataclasses import dataclass, field, replace`)

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_planning_pipeline.py -v`
Expected: 全 PASS。既存テストで plan の rr を検証しているものがあれば導出値に更新。

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/planning_pipeline.py tests/test_planning_pipeline.py
git commit -m "feat(orchestrator): coerce plan rr to derived value, keep LLM claim in agent_outputs"
```

---

### Task 6: draft parse 失敗の redraft 救済 (2.D, planning_pipeline.py)

**Files:**
- Modify: `src/orchestrator/planning_pipeline.py` (draft ループ L199-202 付近)
- Test: `tests/test_planning_pipeline.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_planning_pipeline.py` に追加。parse 失敗は「未知 invalidation type」の draft JSON で再現する:

```python
def _bad_vocab_draft_json() -> str:
    """未知 invalidation type を含む draft (SchemaParseError を誘発)。"""
    return (
        "{"
        '"direction": "long",'
        '"entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],'
        '"action": {"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},'
        '"invalidation": [{"type": "boj_intervention_signal"}],'
        '"expires_at": "2026-12-31T18:00:00+00:00",'
        '"reasoning_summary": "pullback long"'
        "}"
    )


# ── draft parse 救済 (spec 2026-07-16 §2.D) ──────────────────


async def test_draft_parse_error_redrafts_once_then_succeeds(
    store: OrchestratorStore, caplog,
) -> None:
    llm = _ScriptedLLM([OPP_YES, _bad_vocab_draft_json(), _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    with caplog.at_level("WARNING"):
        result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    # 監査ログ (レビュー Low#6): pair / attempt / 例外要約。
    assert any("draft schema parse failed" in r.message for r in caplog.records)
    # 再起案プロンプトに feedback が入っている (3 呼び出し目 = 再 draft の user msg)。
    redraft_user = llm.calls[2][-1]["content"]
    assert "failed schema validation" in redraft_user


async def test_draft_parse_error_twice_fails_safe(store: OrchestratorStore) -> None:
    llm = _ScriptedLLM([OPP_YES, _bad_vocab_draft_json(), _bad_vocab_draft_json()])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "failed"
    assert "SchemaParseError" in result.error


async def test_parse_budget_shared_with_fixable_redraft(store: OrchestratorStore) -> None:
    # parse 救済で redraft 予算 (max_redraft=1) を消費した後の fixable reject は
    # 再起案せず reject 終端 (予算共有)。tp=150.5 → 導出 RR≈0.49 < 1.5。
    llm = _ScriptedLLM([
        OPP_YES, _bad_vocab_draft_json(), _draft_json(tp=150.5), FINAL_ACCEPT,
    ])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "reject"
    assert result.redraft_count == 1


async def test_scan_parse_error_still_fails_safe(store: OrchestratorStore) -> None:
    # scan (PlannerOpportunity) の parse 失敗は救済されない (従来互換)。
    llm = _ScriptedLLM(['{"broken": true}'])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "failed"


async def test_final_parse_error_still_fails_safe(store: OrchestratorStore) -> None:
    # final (PlannerFinalDecision) の parse 失敗も救済されない (従来互換)。
    llm = _ScriptedLLM([OPP_YES, _draft_json(), '{"broken": true}'])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "failed"
```

**既存テストの更新 (必須):** `test_parse_error_yields_failed_no_plan` (L550) は `_ScriptedLLM([OPP_YES, "not json at all"])` の 2 応答構成 — 新実装では 1 回目 parse 失敗後に redraft が走り応答が尽きて IndexError になる。「2 回連続 parse 失敗 → failed + plan なし」形へ更新する:

```python
async def test_parse_error_yields_failed_no_plan(store: OrchestratorStore) -> None:
    # draft parse 失敗は 1 回 redraft 救済される (spec 2026-07-16 §2.D) — 2 回連続で failed。
    llm = _ScriptedLLM([OPP_YES, "not json at all", "still not json"])
    pipe = _make_pipeline(store, llm)
    ctx = _ctx(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")

    result = await pipe.run(pair="USDJPY=X", context=ctx, run_id=run_id)

    assert result.outcome == "failed"
    assert result.plan_id is None
    assert store.get_active_plans("USDJPY=X") == []
```

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_planning_pipeline.py -k "parse" -v`
Expected: 新テストのうち redraft 系が FAIL (現状は 1 回目の parse 失敗で即 failed)

- [ ] **Step 3: 実装**

`src/orchestrator/planning_pipeline.py` — draft ループの `draft = await self._exec.draft(...)` (L199-202) を差し替え:

```python
        while True:
            try:
                draft = await self._exec.draft(
                    pair=pair, direction=direction, context=context,
                    revision_feedback=feedback,
                )
            except SchemaParseError as exc:
                # 監査痕跡 (レビュー Low#6): parse 失敗 draft は _persist_opinion に
                # 到達しないため、構造化ログで pair / attempt / 例外要約を残す
                # (モデル変更後の schema 逸脱率追跡用)。
                logger.warning(
                    "[ORCH] draft schema parse failed for %s (attempt %d): %s",
                    pair, redraft_count + 1, exc,
                )
                if redraft_count < max_redraft:
                    # vocabulary 逸脱等は LLM が直せるミス — fixable reject と同じ
                    # redraft 予算 (max_redraft) で 1 回だけ救済する (spec §2.D)。
                    redraft_count += 1
                    feedback = [
                        f"Previous draft failed schema validation: {exc}. "
                        "Use ONLY the condition vocabularies listed in the schema."
                    ]
                    continue
                raise  # 予算切れ → 従来通り _FAILSAFE_EXC で failed (挙動互換)
            draft = clamp_draft_ttl(draft, max_hours=self._config.plan_ttl_max_hours)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_planning_pipeline.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/planning_pipeline.py tests/test_planning_pipeline.py
git commit -m "feat(orchestrator): rescue draft schema parse failures with one redraft + audit log"
```

---

### Task 7: 統合確認 (per-file suite + 挙動変化表の突合)

**Files:** 変更なし (検証のみ)

- [ ] **Step 1: orchestrator 関連の per-file テストを一括実行**

Run:
```bash
uv run pytest tests/test_orchestrator_schemas.py tests/test_risk_gate_worker.py \
  tests/test_planning_pipeline.py tests/test_orchestrator_config.py \
  tests/test_orchestrator_bootstrap.py tests/test_orchestrator_runtime.py \
  tests/test_orchestrator_e2e.py tests/test_plan_ttl_clamp.py \
  tests/test_config_example_sync.py tests/test_config_loader.py -q
```
Expected: 全 PASS

- [ ] **Step 2: spec §4 挙動変化表との突合**

spec の表の各行に対応するテストが存在することを確認 (Task 1-6 で全行カバー済みのはず):
- 偽 pass 遮断 → `test_overclaimed_rr_rejected_by_derived`
- 誤 reject 解消 → `test_underclaimed_rr_passes_by_derived`
- 申告なし → `test_missing_rr_claim_passes_when_derivable`
- 無条件置換 → `test_rr_claim_within_tolerance_still_replaced`
- underivable → `test_rr_underivable_is_fixable`
- live オーバーシュート → `test_live_overshoot_rejected`
- str/NaN 正規化 → Task 1 の action テスト群
- parse 救済 → Task 6 のテスト群
- min_rr config → Task 4 のテスト群

- [ ] **Step 3: フル suite の参考実行 (回帰判定は per-file)**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: 既知の順序依存フレーク ([[finance_fullsuite_order_flake]]、baseline でも full run のみ ~30 件失敗) を超える新規失敗が無いこと。判断に迷う失敗は単独実行で再確認。

- [ ] **Step 4: Commit (必要なら)**

fixture 調整等の残変更があればコミット:
```bash
git add -A && git commit -m "test: align fixtures with derived-RR gate behavior"
```
