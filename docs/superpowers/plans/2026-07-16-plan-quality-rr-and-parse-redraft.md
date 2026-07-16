# plan 品質バグ修正 (RR 決定的検算 + draft parse 救済) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** risk gate の RR 判定を LLM 申告値から決定的導出値 (entry 候補 min、planning=計画 RR / live=min(計画, 実行) の phase 分離) に置き換え、gate を単一構築で pipeline/runtime に共有し、draft parse 失敗を redraft 1 回で救済する。

**Architecture:** spec `docs/superpowers/specs/2026-07-16-plan-quality-rr-and-parse-redraft-design.md` の §2 (Task 順: 2.A′ → 2.A → 2.B → 2.E → 2.C → 2.D → live 統合テスト → 全体確認)。`derive_rr` は risk_gate.py の module-level 純関数とし、gate と pipeline coerce で共有。`pre_check` は `include_executable_price` keyword (既定なし=必須) で phase を明示: pipeline (planning) = False / runtime の live final gate・shadow precheck = True。gate は bootstrap で 1 個だけ構築し pipeline と runtime に注入する (現行の二重構築を解消、R2 High#2)。

**Tech Stack:** Python 3.11+ / dataclass schema / pytest (asyncio)。テストは既存の per-file 実行が回帰判定 ([[finance_fullsuite_order_flake]]: フル suite は順序依存フレークあり)。

**Base branch:** `feat/technical-llm-omit` (spec コミット済 HEAD の上に積む)。

---

## 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/orchestrator/schemas.py` | `ExecutionPlanDraft.__post_init__` に action の sl/tp/rr 数値正規化 + `_opt_float` の有限値検証 (2.A′) |
| `src/orchestrator/risk_gate.py` | `derive_rr` 追加 (phase 分離・非有限防御) + `pre_check`/`_fixable_issues` に `include_executable_price` + RR 節差し替え (2.A/2.B) |
| `src/orchestrator/runtime.py` | live final gate / shadow precheck の `pre_check` 呼び出しに `include_executable_price=True` (2.B) |
| `src/config/schema.py` | `OrchestratorEntryConfig.min_rr` + `__post_init__` 検証 (2.E) |
| `src/orchestrator/bootstrap.py` | gate 単一構築 (pipeline/runtime 共有) + `min_rr` 接続 (2.E) |
| `config/settings.yaml.example` | `orchestrator.entry.min_rr` 追記 (2.E) |
| `src/orchestrator/planning_pipeline.py` | `pre_check(..., include_executable_price=False)` + rr coerce (2.C) + draft parse 救済 (2.D) |
| `tests/test_orchestrator_schemas.py` | 2.A′ テスト追加 (action + `_opt_float`) |
| `tests/test_risk_gate_worker.py` | derive_rr (phase/NaN 含む) / gate 判定テスト追加・既存テストの keyword 対応 |
| `tests/test_taskf_live_execution_helpers.py` | `_GatePass`/`_GateReject` の `**kwargs` 対応 |
| `tests/test_taskf_execute_live_trigger.py` | 実 gate による live 統合テスト追加 (R2 Medium#4) |
| `tests/test_orchestrator_config.py` | min_rr 設定テスト追加 |
| `tests/test_orchestrator_bootstrap.py` | gate 共有 wiring テスト追加 |
| `tests/test_planning_pipeline.py` | coerce / parse 救済テスト追加 |

---

### Task 1: 数値正規化 (2.A′, schemas.py)

**Files:**
- Modify: `src/orchestrator/schemas.py` (`_opt_float` L58-68 と `ExecutionPlanDraft.__post_init__` L247 付近)
- Test: `tests/test_orchestrator_schemas.py`

**背景:** (a) `action` は未検証 dict。str 数値 (`"149.0"`)、NaN、Infinity、bool が入ると後続の `derive_rr` / 乖離計算が壊れる。`__post_init__` に置くのは runtime の draft 復元 (`_build_execution_draft` のコンストラクタ直呼び) もカバーするため。frozen でない dataclass なので `self.action` の差し替えは可能。(b) `_opt_float` (EntryCondition/InvalidationCondition の value/value_pips が通る共通ヘルパ) も `float(v)` のみで NaN/Inf/bool を素通しする — entry 条件の NaN 候補が derive_rr の min に混入すると `NaN < min_rr` が False になり hard gate を偽通過する (R2 High#3)。

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

`_opt_float` 強化 (R2 High#3) のテストも追加 — `TestEntryCondition` クラス (L30 付近、`test_from_dict_coerces_string_value_to_float` L68 の隣) に:

```python
    def test_from_dict_nan_value_rejected(self) -> None:
        # NaN entry 値は derive_rr の min に混入すると gate を偽通過する (R2 High#3)。
        with pytest.raises(SchemaParseError, match="finite"):
            EntryCondition.from_dict({"type": "price_at_or_below", "value": float("nan")})

    def test_from_dict_infinity_value_rejected(self) -> None:
        with pytest.raises(SchemaParseError, match="finite"):
            EntryCondition.from_dict({"type": "price_at_or_below", "value": float("inf")})

    def test_from_dict_bool_value_rejected(self) -> None:
        with pytest.raises(SchemaParseError, match="numeric"):
            EntryCondition.from_dict({"type": "price_at_or_below", "value": True})
```

`TestInvalidationCondition` クラス (L85 付近) にも:

```python
    def test_from_dict_nan_value_rejected(self) -> None:
        with pytest.raises(SchemaParseError, match="finite"):
            InvalidationCondition.from_dict({"type": "price_below", "value": float("nan")})
```

ファイル冒頭の import に `json` が無ければ追加 (`import json`)。`SchemaParseError` / `pytest` は既存 import 済のはず — 無ければ追加。

- [ ] **Step 2: テストが落ちることを確認**

Run:
```bash
uv run pytest tests/test_orchestrator_schemas.py \
  -k "action or nan_value or infinity_value or bool_value" -v
```
Expected: 上記の reject 系テスト (action 正規化 + `_opt_float` の nan/infinity/bool、R3 Low#3) が FAIL (現状は未検証で通ってしまうため `DID NOT RAISE`)。normalize 系も FAIL (str のまま)。

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

`_opt_float` (L58-68) も強化する:

```python
def _opt_float(value: Any, field_name: str) -> float | None:
    """None はそのまま、それ以外は有限 float 化。失敗時 SchemaParseError。

    LLM が数値を文字列 ("150.0") で返しても正規化する。bool / NaN / Infinity は
    拒否する — NaN の entry 値は derive_rr の min 比較 (NaN < x == False) で
    hard gate を偽通過させるため (R2 High#3)。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaParseError(f"{field_name} must be numeric, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaParseError(f"{field_name} must be numeric, got {value!r}") from exc
    if not math.isfinite(f):
        raise SchemaParseError(f"{field_name} must be finite, got {f!r}")
    return f
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
git commit -m "feat(orchestrator): enforce finite numeric values in draft action and condition schemas"
```

---

### Task 2: derive_rr 純関数 (2.A, risk_gate.py)

**Files:**
- Modify: `src/orchestrator/risk_gate.py`
- Test: `tests/test_risk_gate_worker.py`

**背景:** entry 候補 = price 系 entry_condition の value 全部。実行価格 (long=ask / short=bid、無ければ mid) は `include_executable_price=True` のとき**または** price 系候補ゼロのとき (fallback) のみ追加 — planning 時に実行価格を含めると正常な押し目 plan を誤 reject するため (R2 High#1: 押し目 entry が現在価格から離れているのは正常で、約定は条件成立後)。候補ごとに RR を計算し min を採用。退化候補 (risk<=0 / reward<0) と非有限候補 (NaN/Inf) は rr=0.0 として min に参加 (R2 High#3: NaN 比較 False の偽 pass 防止)。

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
    """derive_rr (spec 2026-07-16 §2.A): entry 候補 min の決定的 RR 導出 + phase 分離。"""

    QUOTE = {"bid": 149.99, "ask": 150.01, "mid": 150.0, "spread": 0.02}

    def test_planning_uses_condition_value_only(self) -> None:
        # planning phase: 条件値 150 のみ評価 → rr=(152-150)/(150-149)=2.0。
        # ask は含めない (押し目誤 reject 防止, R2 High#1)。
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=False)
        assert rr == pytest.approx(2.0)

    def test_live_includes_executable_price(self) -> None:
        # live phase: 候補 = 条件値 150 (rr 2.0) + ask 150.01 (rr≈1.97) → min≈1.97
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=True)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_pullback_plan_planning_pass_live_scenario(self) -> None:
        # R2 High#1 の例: long, 現在 ask=151, 押し目 entry=149.5, SL=148.5, TP=151.5。
        # planning (条件値のみ) → 計画 RR 2.0。実行価格を含めると 0.2 に落ちる —
        # planning で False を渡す限り誤 reject しない。
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=149.5)],
            sl=148.5, tp=151.5,
        )
        quote = {"bid": 150.99, "ask": 151.0, "mid": 150.995, "spread": 0.01}
        assert derive_rr(draft, quote, include_executable_price=False) == pytest.approx(2.0)
        assert derive_rr(draft, quote, include_executable_price=True) == pytest.approx(
            (151.5 - 151.0) / (151.0 - 148.5)
        )

    def test_short_uses_bid_as_executable(self) -> None:
        # short: reward=entry-tp, risk=sl-entry。実行価格は bid。
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_above", value=150.0)],
            direction="short", sl=151.0, tp=148.0,
        )
        rr = derive_rr(draft, self.QUOTE, include_executable_price=True)
        # 候補: 150 → (150-148)/(151-150)=2.0 / bid=149.99 → (1.99)/(1.01)≈1.970
        assert rr == pytest.approx((149.99 - 148.0) / (151.0 - 149.99))

    def test_multiple_price_conditions_takes_min(self) -> None:
        # R1 High#1 の例: long SL=149/TP=152、候補 150 (rr 2.0) と 151 (rr 0.5)
        # → 0.5 を採用 (SL に近い 150 は最良側)。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=151.0),
        ])
        rr = derive_rr(draft, None, include_executable_price=False)
        assert rr == pytest.approx(0.5)

    def test_live_overshoot_executable_price_dominates(self) -> None:
        # R1 High#2 の例: breakout=150, SL=149, TP=152, trigger 時 ask=151.8
        # → 実行 RR=(152-151.8)/(151.8-149)≈0.071 が min。
        draft = _draft_entries([EntryCondition(type="breakout_above", value=150.0)])
        quote = {"bid": 151.78, "ask": 151.8, "mid": 151.79, "spread": 0.02}
        rr = derive_rr(draft, quote, include_executable_price=True)
        assert rr == pytest.approx((152.0 - 151.8) / (151.8 - 149.0))

    def test_degenerate_candidate_counts_as_zero(self) -> None:
        # entry が TP を超えている候補 (reward<0) は rr=0.0 として min に参加。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=152.5),  # > TP
        ])
        assert derive_rr(draft, None, include_executable_price=False) == 0.0

    def test_risk_nonpositive_candidate_counts_as_zero(self) -> None:
        # entry が SL の防御側に無い候補 (long で entry <= sl) は rr=0.0。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=148.5),  # < SL
        ])
        assert derive_rr(draft, None, include_executable_price=False) == 0.0

    def test_nan_quote_counts_as_zero(self) -> None:
        # quote の ask が NaN → その候補は rr=0.0 (reject 方向)。NaN が min 比較を
        # すり抜けて偽 pass しない (R2 High#3)。
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        quote = {"bid": 149.99, "ask": float("nan"), "mid": 150.0, "spread": 0.02}
        assert derive_rr(draft, quote, include_executable_price=True) == 0.0

    def test_no_price_condition_falls_back_to_executable_even_in_planning(self) -> None:
        # price 条件ゼロの draft は planning でも実行価格 fallback (underivable 回避)。
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=False)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_missing_bid_ask_falls_back_to_mid(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, {"mid": 150.0, "spread": None}, include_executable_price=True)
        assert rr == pytest.approx(2.0)

    def test_no_candidates_returns_none(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        assert derive_rr(draft, None, include_executable_price=True) is None
        assert derive_rr(draft, {}, include_executable_price=True) is None

    def test_missing_sl_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], sl=None,
        )
        assert derive_rr(draft, self.QUOTE, include_executable_price=False) is None

    def test_missing_tp_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], tp=None,
        )
        assert derive_rr(draft, self.QUOTE, include_executable_price=False) is None
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

module 冒頭に `import math` を追加し、`_default_pip_size_for` の下 (module-level) に:

```python
def _executable_price(direction: str, quote: dict[str, Any] | None) -> float | None:
    """約定想定価格: long は ask (買い)、short は bid (売り)。無ければ mid。"""
    if not quote:
        return None
    price = quote.get("ask") if direction == "long" else quote.get("bid")
    if price is None:
        price = quote.get("mid")
    return price


def derive_rr(
    draft: ExecutionPlanDraft,
    quote: dict[str, Any] | None,
    *,
    include_executable_price: bool,
) -> float | None:
    """sl/tp/entry 候補から reward/risk 比を決定的に導出する (spec 2026-07-16 §2.A)。

    entry 候補 = price 系 entry_condition の value 全部。実行価格 (long=ask /
    short=bid、無ければ mid) は include_executable_price=True のとき、または
    price 系候補ゼロのとき (underivable 回避 fallback) に追加する。

    phase 分離 (R2 High#1): planning/coerce は False — 押し目 plan の entry が
    現在価格から離れているのは正常で、約定は条件成立後 (watch_evaluator が評価)。
    live final gate / shadow precheck は True — breakout オーバーシュートの
    実行 RR 劣化を trigger 時価格で検出する。

    候補ごとに rr を計算し **最小値** を採用する (保守則 — SL に近い entry ほど
    rr は大きく出るため、min でしか最悪ケースを取れない)。退化候補
    (risk<=0 / reward<0) と非有限候補 (NaN/Inf — quote は schema 層を通らない,
    R2 High#3) は rr 0.0 として min に参加させる (黙って除外すると悪い候補ほど
    無視され、NaN は比較 False で偽 pass する)。

    None (導出不能): sl or tp 欠落 / entry 候補ゼロ。
    """
    sl = draft.action.get("sl")
    tp = draft.action.get("tp")
    if sl is None or tp is None:
        return None

    candidates: list[float] = [
        c.value for c in draft.entry_conditions
        if c.type in _ENTRY_PRICE_TYPES and c.value is not None
    ]
    if include_executable_price or not candidates:
        executable = _executable_price(draft.direction, quote)
        if executable is not None:
            candidates.append(executable)
    if not candidates:
        return None

    def _rr(entry: float) -> float:
        if not (math.isfinite(entry) and math.isfinite(sl) and math.isfinite(tp)):
            return 0.0  # 非有限は reject 方向 (深層防御)
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

### Task 3: gate の RR 判定差し替え + phase 配線 (2.B, risk_gate.py / runtime.py / planning_pipeline.py)

**Files:**
- Modify: `src/orchestrator/risk_gate.py` (`pre_check` L83 / `_fixable_issues` L122, RR 節 L147-152)
- Modify: `src/orchestrator/runtime.py` (L973 live final gate / L1109 shadow precheck)
- Modify: `src/orchestrator/planning_pipeline.py` (L279 `self._risk.pre_check(draft, context)`)
- Modify: `tests/test_taskf_live_execution_helpers.py` (`_GatePass`/`_GateReject` L55-64)
- Test: `tests/test_risk_gate_worker.py`

**背景:** `pre_check` に `include_executable_price: bool` keyword (既定なし = 呼び出し元に選択を強制) を追加。pipeline (planning) = False / runtime の live final gate・shadow precheck = True。shadow precheck も「発注していたら」の判断品質記録なので live と同基準。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_risk_gate_worker.py` に追加 (`worker` fixture は既存 L53: `min_rr=1.5`)。既存 `_draft` は entry=`price_at_or_below 150.0`、`_ctx` は mid=150.0 (ask=150.01):

```python
class TestDerivedRrGate:
    """gate の RR 判定は申告値でなく derive_rr の導出値を使う (spec §2.B)。"""

    def test_overclaimed_rr_rejected_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=3.0 だが計画 RR は (150.5-150.0)/(150.0-149.0)=0.5 (tp=150.5)。
        # 旧実装 (申告比較) では pass していた偽 pass ケース。
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)

    def test_underclaimed_rr_passes_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=0.5 だが計画 RR = 2.0 (>= 1.5)。旧実装では誤 reject していたケース。
        d = _draft(rr=0.5)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is True, res.issues

    def test_missing_rr_claim_passes_when_derivable(self, worker: RiskGateWorker) -> None:
        # 申告 rr 欠落は reject 理由にしない (導出できるため)。missing rr issue は廃止。
        d = _draft(rr=None)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is True, res.issues

    def test_issue_message_includes_claimed(self, worker: RiskGateWorker) -> None:
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
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
        res = worker.pre_check(d, ctx, include_executable_price=True)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("underivable" in i for i in res.issues)

    def test_live_overshoot_rejected(self, worker: RiskGateWorker) -> None:
        # R1 High#2: breakout=150/SL=149/TP=152、trigger 時 ask≈151.8
        # → 実行 RR≈0.07 < 1.5 → live phase (include_executable_price=True) で reject。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="breakout_above", value=150.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        res = worker.pre_check(d, _ctx(mid=151.79), include_executable_price=True)
        assert res.passed is False
        assert any("derived rr" in i for i in res.issues)

    def test_pullback_plan_passes_planning_phase(self, worker: RiskGateWorker) -> None:
        # R2 High#1: 押し目 plan (entry=149.5 が現在 ask≈151 から遠い) は planning
        # phase で誤 reject しない (計画 RR = (151.5-149.5)/(149.5-148.5) = 2.0)。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="price_at_or_below", value=149.5)],
            action={"sl": 148.5, "tp": 151.5, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        res = worker.pre_check(d, _ctx(mid=151.0), include_executable_price=False)
        assert res.passed is True, res.issues
```

- [ ] **Step 2: テストが落ちることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py::TestDerivedRrGate -v`
Expected: FAIL (`pre_check` が keyword を受けず TypeError、または旧実装の申告 rr 比較で assert 失敗)

- [ ] **Step 3: 実装**

`src/orchestrator/risk_gate.py` — `pre_check` (L83) と `_fixable_issues` (L122) のシグネチャに keyword を追加:

```python
    def pre_check(
        self,
        draft: ExecutionPlanDraft,
        context: dict[str, Any],
        *,
        include_executable_price: bool,
    ) -> RiskGateResult:
        """draft を context に対して検証する。

        include_executable_price (spec 2026-07-16 §2.A phase 分離):
          - False = planning phase (pipeline)。計画 RR (price 条件値) で判定。
          - True  = trigger phase (live final gate / shadow precheck)。trigger 時
            実行価格を候補に含め、breakout オーバーシュートを検出する。
        既定値は設けない — 呼び出し元に phase の選択を強制する (暗黙 default は
        planning/live の基準取り違えの再発経路)。
        structural を最優先で判定し、該当すれば即座に structural reject を返す
        (fixable と混在しても structural を返す = §5.3 の「構造的は再起案しない」)。
        """
        structural = self._structural_issues(context)
        if structural:
            return RiskGateResult(passed=False, reject_class=STRUCTURAL, issues=structural)

        fixable = self._fixable_issues(
            draft, context, include_executable_price=include_executable_price
        )
        if fixable:
            return RiskGateResult(passed=False, reject_class=FIXABLE, issues=fixable)

        return RiskGateResult(passed=True, reject_class=None, issues=[])
```

`_fixable_issues` のシグネチャを `def _fixable_issues(self, draft, context, *, include_executable_price: bool) -> list[str]:` に変更し、RR 節 (L147-152) を差し替え:

```python
        # RR: LLM 申告 (action["rr"]) は信用せず derive_rr の導出値で判定する
        # (spec 2026-07-16 §2.B — 申告過大の偽 pass / 申告過小の誤 reject を両方塞ぐ)。
        # 導出不能は楽観通過させず fixable reject (spread unknown と同じ思想)。
        derived = derive_rr(
            draft, context.get("quote"),
            include_executable_price=include_executable_price,
        )
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

呼び出し元 3 箇所 + fake gate を更新:

`src/orchestrator/planning_pipeline.py` L279:

```python
            risk = self._risk.pre_check(draft, context, include_executable_price=False)
```

`src/orchestrator/runtime.py` L973 (live final gate):

```python
            gate = self._risk_gate.pre_check(
                draft, gate_ctx, include_executable_price=True
            )
```

`src/orchestrator/runtime.py` L1109 (shadow precheck):

```python
        return self._risk_gate.pre_check(
            draft, trigger_ctx, include_executable_price=True
        ).to_dict()
```

`tests/test_taskf_live_execution_helpers.py` L55-64 の fake gate に `**kwargs` を追加:

```python
class _GatePass:
    def pre_check(self, draft, context, **kwargs):
        return RiskGateResult(passed=True)


class _GateReject:
    def __init__(self, reject_class):
        self._rc = reject_class

    def pre_check(self, draft, context, **kwargs):
        return RiskGateResult(passed=False, reject_class=self._rc, issues=["x"])
```

- [ ] **Step 4: 既存テストの更新**

keyword 必須化により `tests/test_risk_gate_worker.py` 内の既存 `pre_check(...)` 呼び出しは全て TypeError になる — 機械的に `include_executable_price=False` を付ける (gate 単体テストは planning phase 基準で従来意図を維持)。加えて:

- `test_missing_rr_is_fixable` (L186 付近) は仕様変更で無効。**削除**し、Step 1 の `test_missing_rr_claim_passes_when_derivable` が代替であることを確認。
- `test_rr_below_min_is_fixable` (L140) は申告 rr で下限割れを作っている — 計画 RR も下限割れになるよう修正:

```python
    def test_rr_below_min_is_fixable(self, worker: RiskGateWorker) -> None:
        # 計画 RR = (150.5-150.0)/(150.0-149.0) = 0.5 < 1.5
        res = worker.pre_check(_draft(tp=150.5), _ctx(), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)
```

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_risk_gate_worker.py -v`
Expected: 全 PASS。既存の `test_clean_long_passes` / `test_clean_short_passes` 等も PASS (fixture: entry=150.0 / sl=149.0 / tp=152.0 → 計画 RR 2.0 >= 1.5)。

- [ ] **Step 6: runtime / pipeline / live 執行テストの回帰確認**

Run: `uv run pytest tests/test_orchestrator_runtime.py tests/test_orchestrator_e2e.py tests/test_planning_pipeline.py tests/test_taskf_execute_live_trigger.py tests/test_taskf_live_execution_helpers.py tests/test_watch_counterfactual.py -q`
Expected: 全 PASS。live gate / shadow precheck 経路の fixture が新 RR 判定で reject に変わる場合は、fixture の sl/tp/quote を実行 RR >= 1.5 に調整する (テストの意図は RR でなく遷移検証のため)。`test_watch_counterfactual.py` も pre_check 経由なら同様。

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/risk_gate.py src/orchestrator/runtime.py src/orchestrator/planning_pipeline.py tests/test_risk_gate_worker.py tests/test_taskf_live_execution_helpers.py
git commit -m "feat(orchestrator): risk gate judges derived RR with planning/trigger phase separation"
```

---

### Task 4: min_rr の config 化 + gate 単一構築 (2.E)

**Files:**
- Modify: `src/config/schema.py` (`OrchestratorEntryConfig`, L675-683)
- Modify: `src/orchestrator/bootstrap.py` (`_build_pipeline` L435-459 と `build_orchestrator_runtime` の L293/L310 付近)
- Modify: `config/settings.yaml.example` (L422 の `entry:` ブロック)
- Test: `tests/test_orchestrator_config.py`, `tests/test_orchestrator_bootstrap.py`

**背景 (R2 High#2):** 現行は gate が二重構築 — `_build_pipeline` が pipeline 用を作り、runtime は `risk_gate` 未注入のためコンストラクタ fallback (`runtime.py:172`) で別インスタンス (min_rr=1.5 固定) を作る。config を pipeline 側に繋いでも live final gate に届かない。gate を `build_orchestrator_runtime` で 1 個だけ構築し、`_build_pipeline` へ引数で渡し、`OrchestratorRuntime(risk_gate=...)` にも注入する。

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
def test_risk_gate_shared_and_configured(tmp_path: Path, monkeypatch) -> None:
    """gate は 1 個だけ構築され pipeline と runtime で共有、min_rr は config 値
    (spec 2.E / R2 High#2 — 二重構築だと live final gate に config が届かない)。"""
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
    # 同一インスタンス共有 — runtime fallback (既定 1.5) が生成されていないこと。
    assert rt._risk_gate is rt._pipeline._risk
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

`src/orchestrator/bootstrap.py` — gate を単一構築し pipeline / runtime へ共有する (R2 High#2):

`_build_pipeline` (L435) のシグネチャに `risk_gate` を追加し、内部構築を注入に置き換え:

```python
def _build_pipeline(config: "AppConfig", orch_store: "OrchestratorStore", risk_gate):
    """planning loop の LLM パイプラインを組む。

    (docstring 既存文言のまま、末尾の RiskGateWorker 言及を以下に差し替え)
    RiskGateWorker は build_orchestrator_runtime が単一構築したものを受け取る —
    pipeline (planning) と runtime (live final gate / shadow precheck) で閾値が
    ズレないよう同一インスタンスを共有する (spec 2026-07-16 §2.E)。
    """
    from src.llm.factory import create_agent_llm
    from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent
    from src.orchestrator.planner_agent import PlannerAgent
    from src.orchestrator.planning_pipeline import PlanningPipeline

    planner_llm = create_agent_llm(config, "planner")
    exec_llm = create_agent_llm(config, "execution_opinion")
    return PlanningPipeline(
        orch_store=orch_store,
        planner=PlannerAgent(planner_llm, user_notes_path=config.user_notes_path),
        execution_agent=ExecutionOpinionAgent(exec_llm),
        risk_gate=risk_gate,
        config=config.orchestrator,
    )
```

`build_orchestrator_runtime` 内 — `pipeline = _build_pipeline(config, orch_store)` (L293) の直前で gate を構築し、pipeline と runtime の両方へ渡す:

```python
    # risk gate は単一構築で pipeline (planning) と runtime (live final gate /
    # shadow precheck) に共有する — 二重構築だと config (min_rr 等) が runtime 側
    # fallback に届かず、planning と live で閾値がズレる (spec 2026-07-16 §2.E)。
    from src.orchestrator.risk_gate import RiskGateWorker
    risk_gate = RiskGateWorker(
        min_rr=config.orchestrator.entry.min_rr,
        spread_max_pips=config.orchestrator.entry.spread_max_pips,
    )
    pipeline = _build_pipeline(config, orch_store, risk_gate)
```

`OrchestratorRuntime(...)` 構築 (L310) に `risk_gate=risk_gate,` を追加 (runtime.py のコンストラクタは既に `risk_gate` 引数を受ける — `runtime.py:172` の fallback はテスト用に残す)。

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
git commit -m "feat(orchestrator): single shared risk gate with configurable min_rr"
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
    # 申告 rr=9.9 (乖離>10%) → plan の action.rr は計画 RR (条件値 150 基準、
    # planning phase は実行価格を含めない) = (152-150)/(150-149) = 2.0 に置換。
    llm = _ScriptedLLM([OPP_YES, _draft_json(rr=9.9), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    active = store.get_active_plans("USDJPY=X")
    assert active[0].action_json["rr"] == 2.0


async def test_rr_claim_within_tolerance_still_replaced(store: OrchestratorStore) -> None:
    # 乖離 <=10% でも置換は無条件 (レビュー Medium#3)。ログ発火だけが閾値依存。
    # 申告 2.05 (乖離 2.5%) → 導出 2.0 に置換される。
    llm = _ScriptedLLM([OPP_YES, _draft_json(rr=2.05), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    result = await pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id)
    assert result.outcome == "plan_create"
    active = store.get_active_plans("USDJPY=X")
    assert active[0].action_json["rr"] == 2.0


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
            # underivable reject に委ねる。planning phase なので計画 RR
            # (include_executable_price=False) — plan に保存する rr は計画値で
            # あるべきで、planning 時点の一時的な実勢を焼き込まない。
            derived_rr = derive_rr(
                draft, context.get("quote"), include_executable_price=False
            )
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

### Task 7: live final gate 統合テスト (R2 Medium#4)

**Files:**
- Modify: `tests/test_taskf_live_execution_helpers.py` (`seed_active_plan_ready_to_trigger` L122 に任意引数追加)
- Test: `tests/test_taskf_execute_live_trigger.py` (追加 — production コードは Task 3/4 で変更済み)

**背景:** gate 単体テストでは「runtime に別 gate が生成される」問題 (R2 High#2) や実際の発注抑止・状態遷移を検出できない。`make_live_runtime` (test_taskf_live_execution_helpers) は `risk_gate` を引数で受けるので、fake gate の代わりに**実 RiskGateWorker** を注入して `_execute_live_trigger` 経路を end-to-end で検証する。

**fixture 計算の注意 (R3 Medium#1):** helpers の既定 seed は entry=150.30 / SL=149.4 / TP=151.5 → **計画 RR = 1.2/0.9 ≈ 1.33 < 1.5** で、実 gate では live phase の候補 min = min(1.33, 実行 RR) が必ず 1.33 になり reject される。既定値は変えない (既存テストは fake gate 使用で RR 非依存 — seed の意図を保つ) — 押し目テスト側で entry=150.20 (計画 RR = 1.3/0.8 = 1.625 >= 1.5、mid=150.10 <= 150.20 で trigger 成立) を明示指定する。

- [ ] **Step 1: seed helper に任意引数を追加**

`tests/test_taskf_live_execution_helpers.py` L122 の `seed_active_plan_ready_to_trigger` を後方互換のまま拡張 (R3 Low#2 — 存在しない Store API を使わず helper で条件を注入する):

```python
def seed_active_plan_ready_to_trigger(
    rt: OrchestratorRuntime,
    *,
    entry_conditions: list | None = None,
    action: dict | None = None,
) -> int:
    """entry が即成立する active plan を 1 件作る (既定: price_at_or_below 150.30,
    mid=150.10)。entry_conditions / action を指定すると差し替える (実 gate を使う
    統合テスト用 — 既定 seed の計画 RR ≈ 1.33 は min_rr=1.5 の実 gate を通らない)。"""
    orch = rt._orch
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="swing", direction="long",
        entry_conditions_json=(
            entry_conditions
            if entry_conditions is not None
            else [{"type": "price_at_or_below", "value": 150.30}]
        ),
        action_json=(
            action if action is not None
            else {"sl": 149.4, "tp": 151.5, "rr": 2.0, "confidence": 0.7}
        ),
        invalidation_json=[],
        expires_at=FUTURE, created_by_run_id=run_id,
    )
```

既存呼び出し (引数なし) は挙動不変であることを確認: `uv run pytest tests/test_taskf_execute_live_trigger.py tests/test_watch_counterfactual.py -q` → PASS (Task 3 完了後の状態で)。

- [ ] **Step 2: 統合テストを書く**

`tests/test_taskf_execute_live_trigger.py` に追加。import に実 gate を足す:

```python
from src.orchestrator.risk_gate import RiskGateWorker
```

```python
def test_live_gate_rejects_overshoot_with_real_gate(tmp_path):
    """R2 Medium#4: breakout オーバーシュート時、実 RiskGateWorker の live final gate
    (include_executable_price=True) が発注を止め、intent=abandoned / plan=invalidated
    に遷移する。plan: breakout_above 150.0 / SL 149.0 / TP 152.0 (計画 RR 2.0)、
    trigger 時 mid=151.79 (ask=151.795) → 実行 RR = (152-151.795)/(151.795-149)
    ≈ 0.073 < 1.5。breakout 条件は mid 151.79 > 150.0 で trigger 成立。"""
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    gate = RiskGateWorker(min_rr=1.5, spread_max_pips=2.0)
    rt = make_live_runtime(tmp_path, broker, gate, mid=151.79)
    plan_id = seed_active_plan_ready_to_trigger(
        rt,
        entry_conditions=[{"type": "breakout_above", "value": 150.0}],
        action={"sl": 149.0, "tp": 152.0, "rr": 2.0, "confidence": 0.7},
    )
    rt.run_watch_cycle(now=NOW)
    assert broker.calls == []                          # 発注されない
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "abandoned"                # fixable reject → abandoned
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"


def test_live_gate_passes_pullback_with_real_gate(tmp_path):
    """押し目 plan は trigger 時実行価格が条件値の有利側 → live gate でも pass して
    発注される (live で誤 reject しない回帰)。entry=150.20 / SL=149.4 / TP=151.5:
    計画 RR = (151.5-150.2)/(150.2-149.4) = 1.625、実行 (ask=150.105) RR =
    (151.5-150.105)/(150.105-149.4) ≈ 1.98 → min = 1.625 >= 1.5 で pass。
    trigger は mid 150.10 <= 150.20 で成立 (R3 Medium#1: 既定 seed の entry=150.30
    は計画 RR 1.33 で実 gate を通らないため明示指定)。"""
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    gate = RiskGateWorker(min_rr=1.5, spread_max_pips=2.0)
    rt = make_live_runtime(tmp_path, broker, gate)
    plan_id = seed_active_plan_ready_to_trigger(
        rt,
        entry_conditions=[{"type": "price_at_or_below", "value": 150.20}],
    )
    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == [plan_id]
    assert len(broker.calls) == 1                      # 発注される
    assert rt._orch.get_order_intent(plan_id).status == "filled"
```

**注記 (実装者向け):**
- `make_live_runtime` の quote は `bid=mid-0.005, ask=mid+0.005` (helpers L100 付近) — RR 期待値はこの ask で計算済み。
- overshoot テストの spread = 0.01 / pip 0.01 (JPY) = 1.0 pips < 2.0 なので spread reject は立たない。

- [ ] **Step 3: テストを実行**

Run: `uv run pytest tests/test_taskf_execute_live_trigger.py -v`
Expected: 新テスト 2 件を含め全 PASS (Task 3/4 実装済みが前提。FAIL するなら phase 配線か gate 共有の欠陥 — テストでなく production を疑う)

- [ ] **Step 4: Commit**

```bash
git add tests/test_taskf_execute_live_trigger.py tests/test_taskf_live_execution_helpers.py
git commit -m "test(orchestrator): live final gate integration — overshoot reject, pullback pass"
```

---

### Task 8: 統合確認 (per-file suite + 挙動変化表の突合)

**Files:** 変更なし (検証のみ)

- [ ] **Step 1: orchestrator 関連の per-file テストを一括実行**

Run:
```bash
uv run pytest tests/test_orchestrator_schemas.py tests/test_risk_gate_worker.py \
  tests/test_planning_pipeline.py tests/test_orchestrator_config.py \
  tests/test_orchestrator_bootstrap.py tests/test_orchestrator_runtime.py \
  tests/test_orchestrator_e2e.py tests/test_plan_ttl_clamp.py \
  tests/test_taskf_execute_live_trigger.py tests/test_taskf_live_execution_helpers.py \
  tests/test_taskf_bootstrap_wiring.py tests/test_watch_counterfactual.py \
  tests/test_config_example_sync.py tests/test_config_loader.py -q
```
Expected: 全 PASS

- [ ] **Step 2: spec §4 挙動変化表との突合**

spec の表の各行に対応するテストが存在することを確認 (Task 1-7 で全行カバー済みのはず):
- 偽 pass 遮断 → `test_overclaimed_rr_rejected_by_derived`
- 誤 reject 解消 → `test_underclaimed_rr_passes_by_derived`
- 申告なし → `test_missing_rr_claim_passes_when_derivable`
- 無条件置換 → `test_rr_claim_within_tolerance_still_replaced`
- **押し目 plan の planning pass** → `test_pullback_plan_passes_planning_phase` / `test_pullback_plan_planning_pass_live_scenario`
- underivable → `test_rr_underivable_is_fixable`
- live オーバーシュート (gate 単体) → `test_live_overshoot_rejected`
- **live オーバーシュート (統合: broker 不呼出/abandoned/invalidated)** → `test_live_gate_rejects_overshoot_with_real_gate`
- **live 押し目の発注回帰** → `test_live_gate_passes_pullback_with_real_gate`
- **min_rr の pipeline/runtime 共有** → `test_risk_gate_shared_and_configured`
- str/NaN 正規化 (action + entry 条件) → Task 1 のテスト群
- quote NaN → `test_nan_quote_counts_as_zero`
- parse 救済 → Task 6 のテスト群
- min_rr config 検証 → Task 4 のテスト群

- [ ] **Step 3: フル suite の参考実行 (回帰判定は per-file)**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: 既知の順序依存フレーク ([[finance_fullsuite_order_flake]]、baseline でも full run のみ ~30 件失敗) を超える新規失敗が無いこと。判断に迷う失敗は単独実行で再確認。

- [ ] **Step 4: Commit (必要なら)**

fixture 調整等の残変更があればコミット:
```bash
git add -A && git commit -m "test: align fixtures with derived-RR gate behavior"
```
