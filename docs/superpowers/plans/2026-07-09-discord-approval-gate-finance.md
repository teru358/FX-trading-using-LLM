# Discord 承認ゲート (finance 側 F-1〜F-7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PlannerAgent の plan を人間が Discord で承認してから発注待機させる承認ゲートの finance 側 (status lifecycle / 反実仮想追跡 / API / metrics) を実装する。

**Architecture:** spec `docs/superpowers/specs/2026-07-05-discord-approval-gate.md` (codex 2巡レビュー反映済) の F-1〜F-7。中核は「stamp → 終端時に記録」方式 — `shadow_triggers` の `UNIQUE(plan_id)` を維持したまま、反実仮想 (cf) trigger 行を「real trigger が絶対起こり得ない plan (rejected / unanswered 終端)」にのみ原子 helper で記録する。評価意味論は active と同一、違うのは action 境界のみ。

**Tech Stack:** Python 3.12 / SQLAlchemy ORM (SQLite) / FastAPI / pytest。既存 `OrchestratorStore` / `OrchestratorRuntime` / `PlanningPipeline` の拡張。

**Scope:** finance リポジトリのみ。discord_bot 側 (cog/UI) は本プラン完了後に別プラン (spec §5 の順序)。

---

## 前提・規約

- ブランチ: `feat/planner-watch-loop` (継続)。コミットは日本語 conventional (`feat:` / `test:`)。attribution なし。
- テスト実行 (Windows 側から): `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest <path> -q'`。WSL 内なら直接 `.venv/bin/python -m pytest`。
- テストの時刻 seed は **db_now() 相対** (固定過去日付は date-flake — `tests/test_watch_loop_shadow.py` 冒頭コメント参照)。
- 重要な構造制約 (spec F-4): `shadow_triggers` は `UNIQUE(plan_id)` (orchestrator_store.py の `uq_shadow_triggers_plan_id`)。**pending 中に cf 行を書いてはならない** (後で承認→real trigger と衝突する)。cf 行は terminal (rejected/expired/invalidated) plan にのみ、`finalize_cf_trigger` 経由でのみ書く。
- 執行境界 (spec F-4): live 執行と `triggered` claim は active のみ。cf 経路は `_record_shadow_trigger` / `_execute_live_trigger` / order_intents に一切触れない。

## 対象ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/data/orchestrator_store.py` | PLAN_STATUSES/DECISION_TYPES 拡張、_TradePlan 8列、migration、gate helper 5本、watch-set query 2本、F-5 補助 3本、metrics 集計改修 |
| `src/config/schema.py` | `OrchestratorConfig.approval_gate: bool = False` |
| `src/config/loader.py` | `_build_orchestrator_config` に approval_gate |
| `config/settings.yaml.example` | orchestrator ブロックに approval_gate 行 |
| `src/orchestrator/planning_pipeline.py` | `_commit_plan` の publish 分岐 |
| `src/orchestrator/context_builder.py` | `_build_current_plan` の対象拡張 |
| `src/orchestrator/runtime.py` | `run_watch_cycle` 配線 + `_evaluate_cf_plan` / `_close_cf_window` / `_finalize_cf` |
| `src/orchestrator/shadow_metrics.py` | `ShadowMetrics.gate_labels` |
| `src/orchestrator/shadow_notifier.py` | daily summary に gate 行 |
| `src/orchestrator/plan_view.py` | `plan_to_row` に reasoning 引数 |
| `src/api/_state.py` | `APIState.orchestrator_store` |
| `src/api/server.py` | `start_api_server` 引数 + router 登録 |
| `src/api/routes/orchestrator.py` | **新規** — F-5 endpoint 5本 |
| `main.py` | start_api_server 呼び出しに store 注入 |
| tests | `test_gate_store.py` (新規) / `test_watch_counterfactual.py` (新規) / `test_api_orchestrator_plans.py` (新規) / `test_gate_current_plan.py` (新規) / `test_planning_pipeline.py` (追記) / `test_shadow_metrics.py` (追記) |

---

### Task 1: F-1/F-2 — status・decision type・gate/cf 列の追加

**Files:**
- Modify: `src/data/orchestrator_store.py` (PLAN_STATUSES=line 81 付近 / DECISION_TYPES=line 141 付近 / `_TradePlan`=line 87〜 / `_migrate`=line 323〜)
- Test: `tests/test_gate_store.py` (新規)

- [ ] **Step 1: Write the failing tests**

`tests/test_gate_store.py` を新規作成:

```python
"""approval gate の store 層テスト (spec 2026-07-05-discord-approval-gate.md F-1〜F-4)。

status/列の存在・gate 遷移 helper・stamp/latch/finalize の claim 意味論を
実 SQLite (tmp_path) で検証する。時刻は db_now() 相対 (date-flake 防止)。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


@pytest.fixture
def store(tmp_path: Path) -> OrchestratorStore:
    return OrchestratorStore(tmp_path / "orch.db")


def _plan(store, *, status="pending_approval", pair="USDJPY=X",
          expires_at=None, **kw) -> int:
    """gate テスト用 plan seed。status 指定で直接 ORM 経由 seed する。"""
    snap = store.create_snapshot(pair=pair, as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair=pair)
    return store.create_trade_plan(
        pair=pair, snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=[],
        expires_at=expires_at or (db_now() + timedelta(hours=8)),
        created_by_run_id=run_id, status=status, **kw,
    )


# ── Task 1: status / 列 ────────────────────────────────────────


def test_pending_approval_status_accepted(store):
    plan_id = _plan(store, status="pending_approval")
    assert store.get_trade_plan(plan_id).status == "pending_approval"


def test_rejected_status_accepted(store):
    plan_id = _plan(store, status="pending_approval")
    store.update_plan_status(plan_id, "rejected")
    assert store.get_trade_plan(plan_id).status == "rejected"


def test_gate_and_cf_columns_default_null(store):
    plan = store.get_trade_plan(_plan(store))
    assert plan.gate_decision is None
    assert plan.gate_decided_at is None
    assert plan.gate_reason is None
    assert plan.gate_message_id is None
    assert plan.cf_state is None
    assert plan.cf_stamped_at is None
    assert plan.cf_stamp_price is None
    assert plan.cf_stamp_spread_pips is None


def test_migration_idempotent(tmp_path):
    """同一 DB に対する二重構築で ALTER が落ちない (idempotent)。"""
    db = tmp_path / "orch.db"
    OrchestratorStore(db)
    store2 = OrchestratorStore(db)  # 2回目 — 既存列で例外にならない
    assert store2.get_trade_plan(999) is None  # 通常操作が生きている
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q'`
Expected: FAIL — `test_pending_approval_status_accepted` が `ValueError: status must be one of (...)`、`test_gate_and_cf_columns_default_null` が `AttributeError: gate_decision`

- [ ] **Step 3: Implement**

`src/data/orchestrator_store.py` — 3 箇所:

(a) `PLAN_STATUSES` (line 81 付近) を置換:

```python
PLAN_STATUSES = (
    "active", "triggered", "expired", "invalidated",
    "superseded", "suspended", "requires_replan",
    # approval gate (spec 2026-07-05 F-1): 承認待ち / 却下 (terminal)
    "pending_approval", "rejected",
)
```

(b) `DECISION_TYPES` (line 141 付近) を置換:

```python
DECISION_TYPES = (
    "plan_create", "plan_update", "plan_invalidate",
    "plan_trigger", "direct_hold", "reject",
    # approval gate (F-4): 反実仮想 trigger。real の plan_trigger と集計分離
    "plan_cf_trigger",
)
```

(c) `_TradePlan` のクラス末尾 (`created_at` の直前) に列追加:

```python
    # ── approval gate (spec 2026-07-05 F-2): nullable — gate OFF plan は全て NULL ──
    gate_decision       = Column(String)   # approved | rejected | unanswered (永続ラベル正本)
    gate_decided_at     = Column(DateTime) # 承認/却下の時刻 (放置=unanswered は NULL)
    gate_reason         = Column(String)   # 却下理由 (自由記述・任意)
    gate_message_id     = Column(String)   # Discord message ID (bot 再起動突合)
    # ── 反実仮想追跡 (F-4): stamp → 終端時に記録 ──
    cf_state            = Column(String)   # would_trigger | triggered | invalidated
    cf_stamped_at       = Column(DateTime) # pending 中の entry 初成立時刻
    cf_stamp_price      = Column(Float)    # 同・成立時 mid
    cf_stamp_spread_pips = Column(Float)   # 同・成立時 spread (pips、hindsight 採点用)
```

(d) `_migrate()` の `migrations` リスト末尾に追加:

```python
            # approval gate (spec 2026-07-05 F-2)
            ("trade_plans", "gate_decision", "VARCHAR"),
            ("trade_plans", "gate_decided_at", "DATETIME"),
            ("trade_plans", "gate_reason", "VARCHAR"),
            ("trade_plans", "gate_message_id", "VARCHAR"),
            ("trade_plans", "cf_state", "VARCHAR"),
            ("trade_plans", "cf_stamped_at", "DATETIME"),
            ("trade_plans", "cf_stamp_price", "FLOAT"),
            ("trade_plans", "cf_stamp_spread_pips", "FLOAT"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 4 passed

- [ ] **Step 5: 回帰確認 + Commit**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_orchestrator_store.py tests/test_cli_plans.py -q'`
Expected: all passed (既存 status 検証は tuple 拡張の影響を受けない)

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: approval gate の status/decision type/gate・cf 列を追加 (F-1/F-2)"
```

---

### Task 2: F-2b — try_decide_gate (承認/却下の原子確定)

**Files:**
- Modify: `src/data/orchestrator_store.py` (`try_claim_plan_status` の直後、line 541 付近)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

`tests/test_gate_store.py` に追記:

```python
# ── Task 2: try_decide_gate ───────────────────────────────────


def test_decide_gate_approve_sets_all_fields(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "active"
    assert plan.gate_decision == "approved"
    assert plan.gate_decided_at is not None
    assert plan.gate_reason is None


def test_decide_gate_reject_with_reason(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "rejected", reason="RR悪い") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "rejected"
    assert plan.gate_decision == "rejected"
    assert plan.gate_reason == "RR悪い"


def test_decide_gate_loses_when_not_pending(store):
    """既に決定済み (active) の plan への二重決定は False (API 層で 409)。"""
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    assert store.try_decide_gate(plan_id, "rejected") is False
    # 先勝ちの結果が保持される
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "active"
    assert plan.gate_decision == "approved"


def test_decide_gate_invalid_decision_raises(store):
    plan_id = _plan(store)
    with pytest.raises(ValueError):
        store.try_decide_gate(plan_id, "maybe")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k decide_gate'`
Expected: FAIL — `AttributeError: 'OrchestratorStore' object has no attribute 'try_decide_gate'`

- [ ] **Step 3: Implement**

`src/data/orchestrator_store.py` — `try_mark_plan_triggered` の直後に追加:

```python
    def try_decide_gate(
        self, plan_id: int, decision: str, *, reason: str | None = None,
    ) -> bool:
        """pending_approval の plan に承認/却下を原子的に確定する (gate spec F-2b)。

        status 遷移 (approved→active / rejected→rejected) と gate_decision /
        gate_decided_at / gate_reason を**単一の条件付き UPDATE** で残す。
        try_claim_plan_status では label/時刻/理由を同一 tx で残せないため専用化
        (codex Medium)。二重クリック・承認と却下の競合は rowcount 排他で自然解決 —
        勝てば True、既に pending でなければ False (API 層で 409 に写像)。
        """
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"decision must be 'approved' or 'rejected', got {decision!r}"
            )
        new_status = "active" if decision == "approved" else "rejected"
        now = db_now()
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .values(
                    status=new_status, gate_decision=decision,
                    gate_decided_at=now, gate_reason=reason, updated_at=now,
                )
            )
            session.commit()
            return result.rowcount == 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: try_decide_gate — 承認/却下の status+label+理由を単一UPDATEで原子確定 (F-2b)"
```

---

### Task 3: gate 遷移 helper — unanswered 終端 / stamp / latch

**Files:**
- Modify: `src/data/orchestrator_store.py` (`try_decide_gate` の直後)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

```python
# ── Task 3: unanswered 終端 / stamp / latch ────────────────────


def test_close_pending_unanswered_expired(store):
    plan_id = _plan(store)
    assert store.try_close_pending_unanswered(plan_id, "expired") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert plan.gate_decided_at is None  # 放置は決定時刻なし


def test_close_pending_unanswered_invalidated(store):
    """invalidation による判断なし終端も unanswered (G-3 拡張解釈・内訳は status)。"""
    plan_id = _plan(store)
    assert store.try_close_pending_unanswered(plan_id, "invalidated") is True
    plan = store.get_trade_plan(plan_id)
    assert plan.status == "invalidated"
    assert plan.gate_decision == "unanswered"


def test_close_pending_loses_to_approve_race(store):
    plan_id = _plan(store)
    assert store.try_decide_gate(plan_id, "approved") is True
    assert store.try_close_pending_unanswered(plan_id, "expired") is False
    assert store.get_trade_plan(plan_id).status == "active"


def test_close_pending_invalid_status_raises(store):
    with pytest.raises(ValueError):
        store.try_close_pending_unanswered(_plan(store), "superseded")


def test_stamp_would_trigger_once(store):
    plan_id = _plan(store)
    now = db_now()
    assert store.try_stamp_would_trigger(
        plan_id, at=now, price=150.25, spread_pips=1.2) is True
    plan = store.get_trade_plan(plan_id)
    assert plan.cf_state == "would_trigger"
    assert plan.cf_stamp_price == 150.25
    assert plan.cf_stamp_spread_pips == 1.2
    # dedupe: 2回目は負ける (最初の成立瞬間がエントリー点)
    assert store.try_stamp_would_trigger(
        plan_id, at=now, price=151.0, spread_pips=2.0) is False
    assert store.get_trade_plan(plan_id).cf_stamp_price == 150.25


def test_stamp_requires_pending_status(store):
    """active plan (承認済み) には stamp しない (real 経路の領域)。"""
    plan_id = _plan(store, status="active")
    assert store.try_stamp_would_trigger(
        plan_id, at=db_now(), price=150.0, spread_pips=1.0) is False


def test_latch_cf_invalidated_on_rejected(store):
    plan_id = _plan(store)
    store.try_decide_gate(plan_id, "rejected")
    assert store.try_latch_cf_invalidated(plan_id) is True
    assert store.get_trade_plan(plan_id).cf_state == "invalidated"
    # 冪等: 2回目は負け
    assert store.try_latch_cf_invalidated(plan_id) is False


def test_latch_requires_rejected_status(store):
    plan_id = _plan(store)  # pending のまま
    assert store.try_latch_cf_invalidated(plan_id) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k "close_pending or stamp or latch"'`
Expected: FAIL — AttributeError (3 メソッド未定義)

- [ ] **Step 3: Implement**

`try_decide_gate` の直後に 3 メソッド追加:

```python
    def try_close_pending_unanswered(self, plan_id: int, to_status: str) -> bool:
        """pending_approval を人間の判断なしに終端する (gate spec F-1/F-2b)。

        status (expired=TTL 満了 / invalidated=構造死) と gate_decision='unanswered'
        を単一 UPDATE で刻印。approve/reject との race は rowcount 排他で自然解決。
        gate_decided_at は NULL のまま (放置に決定時刻はない)。
        """
        if to_status not in ("expired", "invalidated"):
            raise ValueError(
                f"to_status must be 'expired' or 'invalidated', got {to_status!r}"
            )
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .values(
                    status=to_status, gate_decision="unanswered",
                    updated_at=db_now(),
                )
            )
            session.commit()
            return result.rowcount == 1

    def try_stamp_would_trigger(
        self, plan_id: int, *, at: datetime, price: float,
        spread_pips: float | None,
    ) -> bool:
        """pending 中の entry 初成立を plan 行に stamp する (gate spec F-4)。

        shadow_triggers には**書かない** — UNIQUE(plan_id) 制約下で、後に承認され
        real trigger が来た時の衝突を構造的に回避する (stamp→終端時に記録 方式)。
        cf_state IS NULL 条件の rowcount claim で初回のみ成立 (dedupe =
        「最初に条件成立した瞬間」がエントリー点)。
        """
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "pending_approval")
                .where(_TradePlan.cf_state.is_(None))
                .values(
                    cf_state="would_trigger", cf_stamped_at=at,
                    cf_stamp_price=price, cf_stamp_spread_pips=spread_pips,
                    updated_at=db_now(),
                )
            )
            session.commit()
            return result.rowcount == 1

    def try_latch_cf_invalidated(self, plan_id: int) -> bool:
        """rejected plan の反実仮想追跡窓を閉じる latch (gate spec F-4)。

        rejected は status が動かないため、invalidation 成立を cf_state='invalidated'
        で記録し以後の entry 評価を止める。latch なしだと「invalidation 成立 →
        価格戻り → entry 成立」で、承認世界なら invalidation で死んでいた plan に
        cf 行が付く (誤った反実仮想)。
        """
        with Session(self._engine) as session:
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status == "rejected")
                .where(_TradePlan.cf_state.is_(None))
                .values(cf_state="invalidated", updated_at=db_now())
            )
            session.commit()
            return result.rowcount == 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: unanswered 終端/would_trigger stamp/cf latch の条件付きUPDATE helper (F-4)"
```

---

### Task 4: finalize_cf_trigger — cf 行の原子確定 helper

**Files:**
- Modify: `src/data/orchestrator_store.py` (Task 3 の直後。import に `or_` / `and_` 追加が必要 — 既存 import 行 `from sqlalchemy import ...` を確認して追記)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

```python
# ── Task 4: finalize_cf_trigger ───────────────────────────────


def _stamped_rejected(store) -> int:
    """stamp 済みで却下された plan (finalize 対象の代表形) を作る。"""
    plan_id = _plan(store)
    store.try_stamp_would_trigger(
        plan_id, at=db_now() - timedelta(minutes=30), price=150.25, spread_pips=1.2)
    store.try_decide_gate(plan_id, "rejected")
    return plan_id


def test_finalize_from_stamp_writes_row_and_hindsight(store):
    plan_id = _stamped_rejected(store)
    trig_id = store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=True, horizon_seconds=3600)
    assert trig_id is not None
    plan = store.get_trade_plan(plan_id)
    assert plan.cf_state == "triggered"
    trig = store.get_shadow_trigger(plan_id)
    assert trig.trigger_price == 150.25          # stamp 値が使われる
    assert trig.spread_pips == 1.2
    assert trig.triggered_at == plan.cf_stamped_at
    # hindsight が enqueue され、過去時刻 triggered_at なので即 ready になる
    ready = store.get_pending_hindsight_evaluations(now=db_now())
    assert any(ev.shadow_trigger_id == trig_id for ev in ready)


def test_finalize_direct_uses_args(store):
    """stamp なし rejected の直接記録 (from=NULL claim)。"""
    plan_id = _plan(store)
    store.try_decide_gate(plan_id, "rejected")
    now = db_now()
    trig_id = store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=False, horizon_seconds=3600,
        triggered_at=now, trigger_price=149.9, spread_pips=0.8)
    assert trig_id is not None
    trig = store.get_shadow_trigger(plan_id)
    assert trig.trigger_price == 149.9
    assert store.get_trade_plan(plan_id).cf_state == "triggered"


def test_finalize_idempotent_second_call_loses(store):
    plan_id = _stamped_rejected(store)
    store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=False, horizon_seconds=3600)
    assert store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=False,
        horizon_seconds=3600) is None


def test_finalize_refuses_non_terminal_plan(store):
    """pending (承認可能) な plan には絶対に cf 行を書かない (UNIQUE 保護)。"""
    plan_id = _plan(store)
    store.try_stamp_would_trigger(
        plan_id, at=db_now(), price=150.0, spread_pips=1.0)
    assert store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=False,
        horizon_seconds=3600) is None
    assert store.get_shadow_trigger(plan_id) is None


def test_finalize_converges_when_row_already_exists(store):
    """trigger 行が既にあるのに cf_state が進んでいない孤児 → 収束する。"""
    plan_id = _stamped_rejected(store)
    plan = store.get_trade_plan(plan_id)
    # 孤児状態を人工的に作る: 行だけ先に存在 (crash-retry 相当)
    store.record_shadow_trigger(
        plan_id=plan_id, decision_id=None, pair=plan.pair, direction=plan.direction,
        triggered_at=plan.cf_stamped_at, trigger_price=plan.cf_stamp_price)
    trig_id = store.finalize_cf_trigger(
        plan_id, decision_id=None, enqueue_hindsight=False, horizon_seconds=3600)
    assert trig_id is not None  # 既存行の id を返す
    assert store.get_trade_plan(plan_id).cf_state == "triggered"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k finalize'`
Expected: FAIL — AttributeError: finalize_cf_trigger

- [ ] **Step 3: Implement**

import 行に `or_` を追加 (ファイル冒頭の `from sqlalchemy import ...` — 既に `select, update, text, func` 等がある行に `or_` を足す)。次に Task 3 の直後へ:

```python
    def finalize_cf_trigger(
        self,
        plan_id: int,
        *,
        decision_id: int | None,
        enqueue_hindsight: bool,
        horizon_seconds: int,
        triggered_at: datetime | None = None,
        trigger_price: float | None = None,
        spread_pips: float | None = None,
        snapshot_id: int | None = None,
    ) -> int | None:
        """反実仮想 trigger 行を単一 transaction で確定する (gate spec F-2b/F-4)。

        (1) cf_state → 'triggered' の rowcount claim (from: 'would_trigger'=stamp 済み
        終端 / NULL=stamp なし rejected の直接記録)、(2) shadow_triggers INSERT、
        (3) hindsight enqueue、を 1 commit で行う。個別 commit の record_shadow_trigger /
        record_hindsight_evaluation は流用しない — 中間クラッシュで「cf_state だけ
        進んで行がない」孤児が出る (codex 2巡目 High)。

        terminal status (rejected/expired/invalidated) 以外では claim が成立しない =
        承認可能な plan に cf 行が書かれることは構造的にない (UNIQUE(plan_id) 保護)。
        triggered_at/trigger_price/spread_pips 省略時は stamp 値を使う。claim 負けは
        None。UNIQUE 違反 = 行が既にある場合は「記録済み」として cf_state を進めて
        既存 id を返す (crash-retry 収束)。
        """
        with Session(self._engine) as session:
            plan = session.get(_TradePlan, plan_id)
            if plan is None:
                logger.warning(f"finalize_cf_trigger: plan {plan_id} not found")
                return None
            at = triggered_at if triggered_at is not None else plan.cf_stamped_at
            price = trigger_price if trigger_price is not None else plan.cf_stamp_price
            spread = spread_pips if spread_pips is not None else plan.cf_stamp_spread_pips
            if at is None:
                logger.warning(
                    f"finalize_cf_trigger: plan {plan_id} has neither stamp nor args"
                )
                return None
            action = plan.action_json if isinstance(plan.action_json, dict) else {}
            result = session.execute(
                update(_TradePlan)
                .where(_TradePlan.plan_id == plan_id)
                .where(_TradePlan.status.in_(("rejected", "expired", "invalidated")))
                .where(or_(
                    _TradePlan.cf_state == "would_trigger",
                    _TradePlan.cf_state.is_(None),
                ))
                .values(cf_state="triggered", updated_at=db_now())
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            trig = _ShadowTrigger(
                plan_id=plan_id, decision_id=decision_id, pair=plan.pair,
                direction=plan.direction, triggered_at=at, trigger_price=price,
                sl=action.get("sl"), tp=action.get("tp"), rr=action.get("rr"),
                spread_pips=spread, snapshot_id=snapshot_id,
                risk_gate_result_json=None,
            )
            session.add(trig)
            try:
                session.flush()  # UNIQUE(plan_id) をここで検出 (commit 前)
            except IntegrityError:
                session.rollback()
                # 行が既に存在 (crash-retry)。記録済みとして cf_state を進め、
                # 既存行 id を返して収束させる。
                with Session(self._engine) as s2:
                    s2.execute(
                        update(_TradePlan)
                        .where(_TradePlan.plan_id == plan_id)
                        .values(cf_state="triggered", updated_at=db_now())
                    )
                    existing = s2.execute(
                        select(_ShadowTrigger)
                        .where(_ShadowTrigger.plan_id == plan_id)
                    ).scalars().first()
                    s2.commit()
                    return existing.id if existing else None
            if enqueue_hindsight:
                session.add(_ShadowHindsightEvaluation(
                    shadow_trigger_id=trig.id, evaluated_at=None, status="pending",
                    horizon_seconds=horizon_seconds, created_at=db_now(),
                ))
            session.commit()
            logger.info(
                f"[ORCH] cf trigger finalized: plan {plan_id} ({plan.pair}) @ {price}"
            )
            return trig.id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: finalize_cf_trigger — cf行のclaim+INSERT+hindsight enqueueを単一txで原子確定 (F-2b/F-4)"
```

---

### Task 5: watch-set query — get_watch_cf_plans / get_cf_finalize_pending

**Files:**
- Modify: `src/data/orchestrator_store.py` (`get_plans_by_status` の直後、line 581 付近。import に `and_` 追加)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

```python
# ── Task 5: watch-set queries ─────────────────────────────────


def test_watch_cf_plans_includes_pending_and_windowed_rejected(store):
    p_pending = _plan(store)
    p_rejected = _plan(store)
    store.try_decide_gate(p_rejected, "rejected")
    ids = {p.plan_id for p in store.get_watch_cf_plans("USDJPY=X")}
    assert ids == {p_pending, p_rejected}


def test_watch_cf_plans_excludes_resolved_and_out_of_window(store):
    # cf 解決済み rejected → 恒久的に外れる
    p_done = _plan(store)
    store.try_decide_gate(p_done, "rejected")
    store.finalize_cf_trigger(
        p_done, decision_id=None, enqueue_hindsight=False, horizon_seconds=3600,
        triggered_at=db_now(), trigger_price=150.0, spread_pips=1.0)
    # latch 済み rejected → 外れる
    p_latched = _plan(store)
    store.try_decide_gate(p_latched, "rejected")
    store.try_latch_cf_invalidated(p_latched)
    # 窓外 (expires_at 過去) rejected → 外れる
    p_old = _plan(store, expires_at=db_now() - timedelta(hours=1))
    store.try_decide_gate(p_old, "rejected")
    # stamp 済み pending は残る (expiry/invalidation 遷移の責務があるため)
    p_stamped = _plan(store)
    store.try_stamp_would_trigger(
        p_stamped, at=db_now(), price=150.0, spread_pips=1.0)
    ids = {p.plan_id for p in store.get_watch_cf_plans("USDJPY=X")}
    assert ids == {p_stamped}


def test_cf_finalize_pending_returns_crashed_terminals(store):
    """status 遷移後・finalize 前にクラッシュした plan を回収対象として返す。"""
    p1 = _plan(store)
    store.try_stamp_would_trigger(p1, at=db_now(), price=150.0, spread_pips=1.0)
    store.try_decide_gate(p1, "rejected")     # rejected + would_trigger (finalize 前)
    p2 = _plan(store)
    store.try_stamp_would_trigger(p2, at=db_now(), price=150.1, spread_pips=1.0)
    store.try_close_pending_unanswered(p2, "expired")  # expired + would_trigger
    ids = {p.plan_id for p in store.get_cf_finalize_pending("USDJPY=X")}
    assert ids == {p1, p2}
    # finalize 後は集合から抜ける
    store.finalize_cf_trigger(
        p1, decision_id=None, enqueue_hindsight=False, horizon_seconds=3600)
    ids = {p.plan_id for p in store.get_cf_finalize_pending("USDJPY=X")}
    assert ids == {p2}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k "watch_cf or finalize_pending"'`
Expected: FAIL — AttributeError (2 メソッド未定義)

- [ ] **Step 3: Implement**

import に `and_` を追加し、`get_plans_by_status` の直後へ:

```python
    def get_watch_cf_plans(self, pair: str) -> list[_TradePlan]:
        """反実仮想 watch の評価対象を返す (gate spec F-4)。

        pending_approval は全件 (expiry/invalidation 遷移の責務があるため stamp 後も
        対象)。rejected は追跡窓内 (expires_at > now) かつ未解決 (cf_state IS NULL)
        のみ — 解決済み・窓閉鎖済みは恒久的に外れ、rejected の蓄積で watch が
        肥大しない。
        """
        now = db_now()
        with Session(self._engine) as session:
            stmt = (
                select(_TradePlan)
                .where(_TradePlan.pair == pair)
                .where(or_(
                    _TradePlan.status == "pending_approval",
                    and_(
                        _TradePlan.status == "rejected",
                        _TradePlan.expires_at > now,
                        _TradePlan.cf_state.is_(None),
                    ),
                ))
            )
            plans = list(session.execute(stmt).scalars().all())
            for p in plans:
                session.expunge(p)
            return plans

    def get_cf_finalize_pending(self, pair: str | None = None) -> list[_TradePlan]:
        """finalize 待ち集合 (crash recovery — gate spec F-4 / codex 2巡目 High)。

        status 遷移 tx と finalize tx の間で落ちた plan (terminal status かつ
        cf_state='would_trigger') を返す。watch tick が finalize_cf_trigger を
        再実行する (claim ベースで冪等) — 復旧不能な取りこぼしを作らない。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_TradePlan)
                .where(_TradePlan.status.in_(("rejected", "expired", "invalidated")))
                .where(_TradePlan.cf_state == "would_trigger")
            )
            if pair is not None:
                stmt = stmt.where(_TradePlan.pair == pair)
            plans = list(session.execute(stmt).scalars().all())
            for p in plans:
                session.expunge(p)
            return plans
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: cf watch対象/finalize待ち集合のstore query (F-4)"
```

---

### Task 6: F-6 — config approval_gate + publish 分岐

**Files:**
- Modify: `src/config/schema.py` (`OrchestratorConfig`、line 726 付近 — `enabled`/`mode` の直後)
- Modify: `src/config/loader.py` (`_build_orchestrator_config`、line 106 付近)
- Modify: `config/settings.yaml.example` (orchestrator ブロック、line 379 付近)
- Modify: `src/orchestrator/planning_pipeline.py` (`_commit_plan` 末尾、line 361 付近)
- Test: `tests/test_planning_pipeline.py` (追記)

- [ ] **Step 1: Write the failing test**

`tests/test_planning_pipeline.py` — `_make_pipeline` (line 112 付近) に config 引数を追加:

```python
def _make_pipeline(
    store: OrchestratorStore, llm, config: OrchestratorConfig | None = None,
) -> PlanningPipeline:
    bundle = AgentLlm(client=llm, temperature=0.1)
    return PlanningPipeline(
        orch_store=store,
        planner=PlannerAgent(bundle),
        execution_agent=ExecutionOpinionAgent(bundle),
        risk_gate=RiskGateWorker(min_rr=1.5, spread_max_pips=2.0, pip_size=0.01),
        config=config or OrchestratorConfig(),
    )
```

ファイル末尾にテスト追加 (**注**: 同ファイルの既存テストが `pipe.run` を `await` (pytest-asyncio) で呼んでいるか `asyncio.run` かを確認し、同じ呼び方に合わせる):

```python
def test_gate_on_publishes_pending_approval(store):
    """gate ON: create は requires_replan のまま、最後の publish だけ pending_approval。

    write 順序 (create→decision→vote→supersede→publish) = orphan 防止は gate に
    依らず不変 (gate spec F-6 / codex High)。
    """
    import asyncio
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm, config=OrchestratorConfig(approval_gate=True))
    run_id = store.start_run("test", pair="USDJPY=X")
    result = asyncio.run(pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id))
    assert result.outcome == "plan_create"
    plan = store.get_trade_plan(result.plan_id)
    assert plan.status == "pending_approval"
    assert plan.gate_decision is None          # まだ判断なし
    assert result.decision_ids                 # plan_create decision が存在 (順序不変)


def test_gate_off_publishes_active_unchanged(store):
    """gate OFF (既定): 現行どおり active で publish — 挙動不変。"""
    import asyncio
    llm = _ScriptedLLM([OPP_YES, _draft_json(), FINAL_ACCEPT])
    pipe = _make_pipeline(store, llm)
    run_id = store.start_run("test", pair="USDJPY=X")
    result = asyncio.run(pipe.run(pair="USDJPY=X", context=_ctx(store), run_id=run_id))
    assert store.get_trade_plan(result.plan_id).status == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_planning_pipeline.py -q -k gate'`
Expected: FAIL — `TypeError: OrchestratorConfig.__init__() got an unexpected keyword argument 'approval_gate'`

- [ ] **Step 3: Implement**

(a) `src/config/schema.py` — `OrchestratorConfig` の `mode` 行の直後:

```python
    # approval gate (spec 2026-07-05): ON のとき plan の publish が pending_approval
    # になり、人間の承認 (API approve) を経てから active 化する。既定 OFF = 挙動不変。
    approval_gate: bool = False
```

(b) `src/config/loader.py` — `_build_orchestrator_config` の `mode=...` 行の直後:

```python
        approval_gate=data.get("approval_gate", False),
```

(c) `config/settings.yaml.example` — `mode: shadow  # shadow | live` 行の直後:

```yaml
  # 承認ゲート (spec 2026-07-05): true で plan が pending_approval で公開され、
  # Discord 承認 (API approve) を経てから発注待機 (active) になる。既定 false。
  approval_gate: false
```

(d) `src/orchestrator/planning_pipeline.py` — `_commit_plan` の publish 行を置換:

変更前:
```python
        # 全 write 成功 → ここで初めて active 化 (orphan window を閉じる)。
        self._orch.update_plan_status(plan_id, "active")
```

変更後:
```python
        # 全 write 成功 → ここで初めて publish (orphan window を閉じる)。gate ON なら
        # active でなく pending_approval で公開し、人間の承認を待つ (gate spec F-6)。
        # 分岐は publish の 1 箇所のみ — create(requires_replan)→decision→vote→
        # supersede の write 順序は gate に依らず不変 (codex High)。
        publish_status = (
            "pending_approval" if self._config.approval_gate else "active"
        )
        self._orch.update_plan_status(plan_id, publish_status)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_planning_pipeline.py -q'`
Expected: all passed (既存含む)

- [ ] **Step 5: Commit**

```bash
git add -f src/config/schema.py src/config/loader.py config/settings.yaml.example src/orchestrator/planning_pipeline.py tests/test_planning_pipeline.py
git commit -m "feat: approval_gate config + pipeline publish分岐 — gate ONでpending_approval公開 (F-6)"
```

---

### Task 7: F-3 — supersede の対象拡張

**Files:**
- Modify: `src/data/orchestrator_store.py` (`supersede_active_plans`、line 1256 付近)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

```python
# ── Task 7: supersede 拡張 ────────────────────────────────────


def test_supersede_includes_pending_approval(store):
    """新 plan 作成時、返答待ち plan も置換される (G-6)。判断なし = gate_decision NULL。"""
    p_pending = _plan(store)
    ids = store.supersede_active_plans("USDJPY=X")
    assert ids == [p_pending]
    plan = store.get_trade_plan(p_pending)
    assert plan.status == "superseded"
    assert plan.gate_decision is None  # rejected ではない (人間の判断なし)


def test_supersede_excludes_rejected(store):
    """rejected は terminal のまま置換対象外 (追跡窓は expires_at まで継続)。"""
    p_rej = _plan(store)
    store.try_decide_gate(p_rej, "rejected")
    ids = store.supersede_active_plans("USDJPY=X")
    assert ids == []
    assert store.get_trade_plan(p_rej).status == "rejected"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k supersede'`
Expected: FAIL — `test_supersede_includes_pending_approval` で ids == [] (pending が対象外のため)

- [ ] **Step 3: Implement**

`supersede_active_plans` の where 句を変更:

変更前:
```python
                .where(_TradePlan.status == "active")
```

変更後:
```python
                # gate spec F-3: 返答待ち plan も新 plan で置換する (G-6)。置換された
                # pending は superseded (gate_decision NULL = 人間の判断なしラベル)。
                # rejected は terminal のまま対象外。
                .where(_TradePlan.status.in_(("active", "pending_approval")))
```

docstring 1 行目も「pair の active/pending_approval plan を全て status='superseded' にする。」に更新。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上 + `tests/test_planning_pipeline.py -q` (supersede を使う既存テストの回帰確認)
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: supersede対象に pending_approval を追加 — rejected は terminal のまま (F-3)"
```

---

### Task 8: F-1 — current_plan context の対象拡張

**Files:**
- Modify: `src/orchestrator/context_builder.py` (`_build_current_plan`、line 298 付近)
- Test: `tests/test_gate_current_plan.py` (新規)

- [ ] **Step 1: Write the failing test**

`tests/test_gate_current_plan.py` を新規作成:

```python
"""current_plan ブロックの gate 拡張テスト (gate spec F-1)。

返答待ち (pending_approval) plan も planner から見える — 承認待ちの間に
同 pair の重複 plan を planner が知らずに乱発しないため。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder
from src.utils.clock import db_now


def _builder(tmp_path: Path) -> tuple[DecisionContextBuilder, OrchestratorStore]:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    return DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig()), orch


def _seed_plan(orch, *, status: str) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status=status,
    )


def test_current_plan_sees_pending_approval(tmp_path):
    builder, orch = _builder(tmp_path)
    _seed_plan(orch, status="pending_approval")
    assert builder._build_current_plan("USDJPY=X") is not None


def test_current_plan_ignores_rejected(tmp_path):
    builder, orch = _builder(tmp_path)
    _seed_plan(orch, status="rejected")
    assert builder._build_current_plan("USDJPY=X") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_current_plan.py -q'`
Expected: `test_current_plan_sees_pending_approval` FAIL (None が返る)

- [ ] **Step 3: Implement**

`src/orchestrator/context_builder.py` `_build_current_plan` 内の取得行を変更:

変更前:
```python
            plans = self._orch.get_active_plans(pair)
```

変更後:
```python
            # gate spec F-1: 返答待ち plan も planner から見える (承認待ち中の重複
            # 起案を防ぐ)。supersede により pair 単位で active+pending は最大 1 件。
            plans = self._orch.get_plans_by_status(
                ("active", "pending_approval"), pair
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上 + `tests/test_context_builder_position.py tests/test_context_builder.py -q` (回帰)
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -f src/orchestrator/context_builder.py tests/test_gate_current_plan.py
git commit -m "feat: current_plan が pending_approval も参照 — 承認待ち中の重複起案防止 (F-1)"
```

---

### Task 9: F-4a — runtime 配線 + pending の entry stamp

**Files:**
- Modify: `src/orchestrator/runtime.py` (`run_watch_cycle`=line 293 付近 / 新メソッドは `_evaluate_plan` の直後)
- Test: `tests/test_watch_counterfactual.py` (新規)

**参照 (実装前に読む):** `src/orchestrator/runtime.py` の `_evaluate_plan` (line 517〜) と `_record_shadow_trigger` (line 643〜) — 評価順序と `_spread_pips` / `_side_of` の使い方。fixture は `tests/test_watch_loop_shadow.py` のパターン踏襲。

- [ ] **Step 1: Write the failing tests**

`tests/test_watch_counterfactual.py` を新規作成:

```python
"""反実仮想 watch (gate spec F-4) のテスト。

評価意味論は active と同一・action 境界のみ違う:
- pending: entry 初成立 → stamp のみ (shadow 行なし・plan は pending のまま承認可能)
- pending: expiry/invalidation → unanswered 終端 (+stamp 済なら cf finalize)
- rejected: entry → cf 行を直接 finalize / invalidation → latch
- 執行境界: live mode + broker 注入でも cf 経路は執行しない
fixture は tests/test_watch_loop_shadow.py のパターン (db_now() 相対) を踏襲。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.price_analyzer import PriceAnalysis
from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime
from src.utils.clock import db_now

NOW = db_now()
FUTURE = NOW + timedelta(hours=8)
PAST = NOW - timedelta(hours=1)


def _seed_ok_technical(db: Path) -> None:
    """freshness final wall を通す ok technical snapshot (age=60s で fresh)。"""
    AnalysisStore(db).add_snapshot(
        PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.7,
            entry_zone=(149.0, 151.0), reasoning_summary="seed",
            analyzed_at=NOW - timedelta(seconds=60),
        )
    )


def _make_runtime(
    tmp_path: Path, *, mid: float, spread: float = 0.01,
    seed_technical: bool = False, mode: str = "shadow", execution_broker=None,
) -> OrchestratorRuntime:
    db = tmp_path / "orch.db"
    orch = OrchestratorStore(db)
    if seed_technical:
        _seed_ok_technical(db)
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=mid - spread / 2, ask=mid + spread / 2, mid=mid, spread=spread,
            source="test", observed_at=NOW,
        )

    return OrchestratorRuntime(
        config=OrchestratorConfig(),
        orch_store=orch,
        context_builder=builder,
        pairs=["USDJPY=X"],
        quote_provider=quote_provider,
        mode=mode,
        execution_broker=execution_broker,
    )


def _create_gate_plan(
    orch: OrchestratorStore, *, status: str = "pending_approval",
    entry: list[dict] | None = None, invalidation: list[dict] | None = None,
    expires_at: datetime = FUTURE,
) -> int:
    snap = orch.create_snapshot(pair="USDJPY=X", as_of_time=NOW)
    run_id = orch.start_run("PlannerAgent", pair="USDJPY=X")
    return orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=entry or [{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0},
        invalidation_json=invalidation or [],
        expires_at=expires_at, created_by_run_id=run_id, status=status,
    )


# ── Task 9: pending の entry stamp ────────────────────────────


def test_pending_entry_stamps_without_shadow_row(tmp_path):
    """entry 成立 → stamp のみ。shadow 行なし・status は pending のまま (承認可能)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)

    triggered = rt.run_watch_cycle(now=NOW)

    assert triggered == []                       # real trigger には数えない
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "pending_approval"     # 承認可能なまま
    assert plan.cf_state == "would_trigger"
    assert plan.cf_stamp_price == 150.10
    assert plan.cf_stamped_at is not None
    assert rt._orch.get_shadow_trigger(plan_id) is None  # 行は書かない (UNIQUE 保護)


def test_pending_entry_not_hold_no_stamp(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.50)  # 閾値の上 — 未成立
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state is None


def test_pending_stamp_only_once(tmp_path):
    """2 tick 目は stamp を上書きしない (最初の成立瞬間がエントリー点)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    first = rt._orch.get_trade_plan(plan_id).cf_stamped_at
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))
    assert rt._orch.get_trade_plan(plan_id).cf_stamped_at == first


def test_pending_freshness_wall_blocks_stamp(tmp_path):
    """freshness 失敗時は stamp しない (active と同じ物差し)。technical seed なし。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=False)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state is None


def test_stamped_pending_approved_then_real_trigger_no_conflict(tmp_path):
    """stamp 済み pending を承認 → active → real trigger が UNIQUE 衝突なく記録される。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)                       # stamp
    assert rt._orch.try_decide_gate(plan_id, "approved") is True
    triggered = rt.run_watch_cycle(now=NOW + timedelta(seconds=2))  # real trigger
    assert triggered == [plan_id]
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "triggered"
    assert plan.cf_state == "would_trigger"           # stamp は残る (承認遅延コスト素材)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None                           # real 行 1 本のみ
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_watch_counterfactual.py -q'`
Expected: FAIL — stamp 系 4 件 (cf_state None のまま)。`test_stamped_pending_approved...` は approve までは通るが stamp assert で FAIL

- [ ] **Step 3: Implement**

(a) `src/orchestrator/runtime.py` `run_watch_cycle` の pair ループ内、active plan ループの直後に追加:

```python
            # ── gate spec F-4: 反実仮想 watch (pending_approval / rejected) ──
            # 評価意味論は active と同一・action 境界のみ違う (記録専用・執行なし)。
            for plan in self._orch.get_watch_cf_plans(pair):
                try:
                    self._evaluate_cf_plan(plan, pair, now)
                except Exception:
                    logger.exception(
                        f"[ORCH] cf watch eval failed for plan {plan.plan_id} ({pair})"
                    )
            # finalize 待ち回収 (status 遷移と finalize の間のクラッシュ復旧、冪等)。
            for plan in self._orch.get_cf_finalize_pending(pair):
                try:
                    self._finalize_cf(plan, pair, now)
                except Exception:
                    logger.exception(
                        f"[ORCH] cf finalize recovery failed for plan {plan.plan_id}"
                    )
```

(b) `_evaluate_plan` の直後に 3 メソッド追加 (このタスクでは `_evaluate_cf_plan` の entry/stamp 部分と `_finalize_cf` / `_close_cf_window` の全体を入れる — Task 10/11 のテストが同メソッドの残り分岐を検証する):

```python
    def _evaluate_cf_plan(self, plan, pair: str, now: datetime) -> None:
        """非 active plan (pending_approval / rejected) の反実仮想評価 (gate spec F-4)。

        _evaluate_plan と同一の評価意味論 (invalidation/expiry → entry → freshness) で、
        action だけが違う:
        - pending: expiry/invalidation → unanswered 終端 (+stamp 済なら cf finalize) /
          entry 初成立 → stamp のみ (shadow 行は書かない — UNIQUE(plan_id) 衝突回避)
        - rejected: invalidation → cf_state latch / entry 初成立 → cf 行を直接 finalize
        執行経路 (_record_shadow_trigger / _execute_live_trigger / order_intents) には
        一切入らない (spec F-4 執行境界)。
        """
        quote = self._quote_provider(pair)
        ctx = self._ctx.assemble(pair=pair, now=now, quote=quote)
        self._enrich_ages(ctx, now)
        ctx["news_conflict"] = self._news_conflicts(ctx, plan.direction)

        is_pending = plan.status == "pending_approval"
        try:
            entry_conds = [
                EntryCondition.from_dict(c) for c in (plan.entry_conditions_json or [])
            ]
            inval_conds = [
                InvalidationCondition.from_dict(c) for c in (plan.invalidation_json or [])
            ]
        except SchemaParseError as exc:
            logger.warning(
                f"[ORCH] cf plan {plan.plan_id} ({pair}) has unparseable conditions: {exc}"
            )
            self._close_cf_window(plan, pair, "unparseable_conditions", now)
            return

        # 1. expiry / invalidation 最優先 (active と同一意味論)。
        reason = self._evaluator.invalidation_reason(
            inval_conds, ctx, now=now, expires_at=plan.expires_at
        )
        if reason is not None:
            self._close_cf_window(plan, pair, reason, now)
            return

        # 2. entry 未成立なら何もしない。
        if not self._evaluator.entry_conditions_hold(entry_conds, ctx):
            return

        # stamp 済み pending は entry 記録済み — 以後は expiry/invalidation 監視のみ。
        if is_pending and plan.cf_state == "would_trigger":
            return

        # 3. freshness final wall (active と同一 — 承認済みとの比較が同じ物差し)。
        issues = self._evaluator.freshness_issues(ctx)
        if issues:
            self._orch.record_freshness(
                snapshot_id=plan.snapshot_id, pair=pair, issues=issues
            )
            return

        # 4. action 境界 (ここだけ active と違う)。
        if is_pending:
            if self._orch.try_stamp_would_trigger(
                plan.plan_id, at=now, price=quote.mid,
                spread_pips=self._spread_pips(pair, quote.spread),
            ):
                logger.info(
                    f"[ORCH] ⚡ would-trigger stamped: plan {plan.plan_id} {pair} "
                    f"{plan.direction} @ {quote.mid} (pending approval)"
                )
        else:  # rejected — terminal なので直接 cf finalize (from=NULL claim)
            self._finalize_cf(
                plan, pair, now,
                trigger_price=quote.mid,
                spread_pips=self._spread_pips(pair, quote.spread),
            )

    def _close_cf_window(self, plan, pair: str, reason: str, now: datetime) -> None:
        """cf 追跡窓の終端処理 (gate spec F-4 の expiry/invalidation 列)。

        pending: real 遷移 (expired/invalidated) + gate_decision='unanswered' を単一
        UPDATE で刻印し plan_invalidate decision を記録。stamp 済なら cf finalize
        (「承認待ち中に entry できたのに判断前に死んだ」— gate 遅延サンプル、捨てない)。
        rejected: status は動かさず cf_state='invalidated' の latch のみ (expiry は
        watch クエリの expires_at 条件で自然に外れるため何もしない)。
        """
        if plan.status == "rejected":
            if reason != "expired":
                if self._orch.try_latch_cf_invalidated(plan.plan_id):
                    logger.info(
                        f"[ORCH] cf window latched (invalidated): plan {plan.plan_id} "
                        f"({pair}): {reason}"
                    )
            return

        # pending_approval → expired / invalidated (+unanswered 刻印、race は rowcount)
        to_status = "expired" if reason == "expired" else "invalidated"
        if not self._orch.try_close_pending_unanswered(plan.plan_id, to_status):
            return  # approve/reject に負けた — 何もしない
        run_id = self._orch.start_run(
            "OrchestratorRuntime", pair=pair, trigger_type="watch_cycle",
        )
        ok = False
        try:
            self._orch.record_decision(
                run_id=run_id, snapshot_id=plan.snapshot_id, pair=pair,
                decision_type="plan_invalidate", plan_id=plan.plan_id,
                reasoning_summary=f"pending gate close: {reason}",
            )
            ok = True
            logger.info(
                f"[ORCH] pending plan {plan.plan_id} ({pair}) {to_status} "
                f"(unanswered): {reason}"
            )
        finally:
            self._orch.finish_run(run_id, status="ok" if ok else "failed")
        # stamp 済なら同 tick で cf finalize (crash 時は finalize 待ち集合が回収)。
        if plan.cf_state == "would_trigger":
            self._finalize_cf(plan, pair, now)

    def _finalize_cf(
        self, plan, pair: str, now: datetime,
        *, trigger_price: float | None = None, spread_pips: float | None = None,
    ) -> None:
        """cf trigger を decision 付きで確定する (gate spec F-4)。

        trigger_price 省略時は stamp 値を使う (stamp 済み終端 / finalize 待ち回収)。
        decision (plan_cf_trigger) は finalize tx の外 — 失敗しても cf 行の原子性には
        影響しない (trace のみ。crash-retry で稀に重複し得るが append-only として許容)。
        """
        run_id = self._orch.start_run(
            "OrchestratorRuntime", pair=pair, trigger_type="watch_counterfactual",
        )
        ok = False
        try:
            decision_id = self._orch.record_decision(
                run_id=run_id, snapshot_id=plan.snapshot_id, pair=pair,
                decision_type="plan_cf_trigger", decision=_side_of(plan.direction),
                plan_id=plan.plan_id,
                reasoning_summary=f"counterfactual trigger ({plan.status})",
            )
            trig_id = self._orch.finalize_cf_trigger(
                plan.plan_id,
                decision_id=decision_id,
                enqueue_hindsight=self._hindsight is not None,
                horizon_seconds=self._config.hindsight.horizon_seconds,
                triggered_at=now if trigger_price is not None else None,
                trigger_price=trigger_price,
                spread_pips=spread_pips,
            )
            ok = True
            if trig_id is not None:
                logger.info(
                    f"[ORCH] 🧪 cf trigger plan {plan.plan_id} {pair} "
                    f"{plan.direction} ({plan.status})"
                )
        finally:
            self._orch.finish_run(run_id, status="ok" if ok else "failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_watch_counterfactual.py tests/test_watch_loop_shadow.py -q'`
Expected: all passed (既存 watch テスト回帰含む)

- [ ] **Step 5: Commit**

```bash
git add -f src/orchestrator/runtime.py tests/test_watch_counterfactual.py
git commit -m "feat: 反実仮想watch配線 + pendingのentry stamp — 評価意味論はactiveと同一 (F-4a)"
```

---

### Task 10: F-4b — pending の unanswered 終端 (expiry / invalidation)

**Files:**
- Modify: なし (Task 9 で実装済みの `_close_cf_window` 経路を検証)
- Test: `tests/test_watch_counterfactual.py` (追記)

- [ ] **Step 1: Write the tests (Task 9 実装のカバレッジ確認 — RED にならなければ Task 9 の実装漏れ検出)**

```python
# ── Task 10: pending の unanswered 終端 ───────────────────────


def test_pending_expiry_marks_unanswered_no_stamp(tmp_path):
    """stamp なしで TTL 到達 → expired + unanswered、cf 行なし。"""
    rt = _make_runtime(tmp_path, mid=150.50)  # entry 未成立の価格
    plan_id = _create_gate_plan(rt._orch, expires_at=PAST)
    rt.run_watch_cycle(now=NOW)
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_pending_expiry_with_stamp_finalizes_cf(tmp_path):
    """stamp → TTL 到達 → expired + unanswered + cf 行 (triggered_at=stamp 時刻)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _create_gate_plan(rt._orch, expires_at=NOW + timedelta(seconds=30))
    rt.run_watch_cycle(now=NOW)                          # stamp
    stamped_at = rt._orch.get_trade_plan(plan_id).cf_stamped_at
    rt.run_watch_cycle(now=NOW + timedelta(minutes=5))   # TTL 超過
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "expired"
    assert plan.gate_decision == "unanswered"
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.triggered_at == stamped_at               # エントリー点は stamp 時刻
    assert trig.trigger_price == 150.10


def test_pending_invalidation_marks_unanswered(tmp_path):
    """invalidation 成立 → invalidated + unanswered (G-3 拡張解釈)。承認不能になる。"""
    rt = _make_runtime(tmp_path, mid=147.50)  # invalidation (price_below 148) が成立
    plan_id = _create_gate_plan(
        rt._orch,
        entry=[{"type": "price_at_or_below", "value": 147.0}],  # entry は未成立
        invalidation=[{"type": "price_below", "value": 148.0}],
    )
    rt.run_watch_cycle(now=NOW)
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "invalidated"
    assert plan.gate_decision == "unanswered"
    # 承認不能 (409 相当)
    assert rt._orch.try_decide_gate(plan_id, "approved") is False
```

- [ ] **Step 2: Run tests**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_watch_counterfactual.py -q'`
Expected: all passed (Task 9 の実装が正しければ GREEN。FAIL したら `_close_cf_window` を修正)

- [ ] **Step 3: Commit**

```bash
git add -f tests/test_watch_counterfactual.py
git commit -m "test: pending の unanswered 終端 (expiry/invalidation) の検証 (F-4b)"
```

---

### Task 11: F-4c — rejected の直接 cf finalize + latch + 執行境界

**Files:**
- Modify: なし (Task 9 実装の検証。FAIL 時は runtime を修正)
- Test: `tests/test_watch_counterfactual.py` (追記)

- [ ] **Step 1: Write the tests**

```python
# ── Task 11: rejected の cf finalize / latch / 執行境界 ────────


def _rejected_plan(rt, **kw) -> int:
    plan_id = _create_gate_plan(rt._orch, **kw)
    assert rt._orch.try_decide_gate(plan_id, "rejected") is True
    return plan_id


def test_rejected_entry_finalizes_cf_directly(tmp_path):
    """reject 後の entry 初成立 → cf 行 (status は rejected のまま不変)。"""
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _rejected_plan(rt)
    triggered = rt.run_watch_cycle(now=NOW)
    assert triggered == []                     # real trigger には数えない
    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.status == "rejected"           # terminal のまま
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.trigger_price == 150.10
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_cf_trigger"   # real と集計分離


def test_rejected_cf_recorded_once(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.10, seed_technical=True)
    plan_id = _rejected_plan(rt)
    rt.run_watch_cycle(now=NOW)
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))  # 2 tick 目
    # 行は 1 本のまま (UNIQUE + cf_state 解決済みで watch 対象から外れる)
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None


def test_rejected_invalidation_latches_then_entry_ignored(tmp_path):
    """invalidation latch 後は entry が成立しても cf 行を書かない (誤反実仮想の防止)。"""
    # tick1: mid=147.5 → invalidation (price_below 148) 成立 → latch
    rt = _make_runtime(tmp_path, mid=147.50, seed_technical=True)
    plan_id = _rejected_plan(
        rt,
        entry=[{"type": "price_at_or_below", "value": 147.0}],
        invalidation=[{"type": "price_below", "value": 148.0}],
    )
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state == "invalidated"
    # tick2: 価格が戻って entry 成立圏 (146.9) でも記録しない
    rt2_quote_mid = 146.90
    rt._quote_provider = lambda pair: QuoteSnapshot(
        bid=rt2_quote_mid - 0.005, ask=rt2_quote_mid + 0.005, mid=rt2_quote_mid,
        spread=0.01, source="test", observed_at=NOW,
    )
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))
    assert rt._orch.get_shadow_trigger(plan_id) is None


def test_cf_paths_never_execute_in_live_mode(tmp_path):
    """live mode + broker 注入でも cf 経路は執行しない (spec F-4 執行境界)。"""
    broker = MagicMock()
    rt = _make_runtime(
        tmp_path, mid=150.10, seed_technical=True,
        mode="live", execution_broker=broker,
    )
    _rejected_plan(rt)
    pending_id = _create_gate_plan(rt._orch)
    rt.run_watch_cycle(now=NOW)
    # rejected は cf 行化・pending は stamp、どちらも broker に触れない
    broker.assert_not_called()
    assert not broker.method_calls
    assert rt._orch.get_order_intent(pending_id) is None
```

**注1**: `get_order_intent` が store に無い場合 (メソッド名が違う場合) は `grep -n "def get_order_intent\|def get_order" src/data/orchestrator_store.py` で実名を確認して置換する。存在しなければ `broker.method_calls` の assert だけで足りる (order_intent は broker 経由でのみ作られるため)。

**注2**: `OrchestratorRuntime(mode="live", execution_broker=...)` が init 時に追加依存 (execution_position_mgr / app_config 等) を要求して落ちる場合は、`grep -n "mode == .live." src/orchestrator/runtime.py | head` で検証箇所を確認し、`execution_position_mgr=MagicMock()` 等の最小 stub を `_make_runtime` に追加する (テストの主張は「cf 経路が broker に触れない」— init 要件は本質でない)。

- [ ] **Step 2: Run tests**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_watch_counterfactual.py -q'`
Expected: all passed (FAIL 時は Task 9 の `_evaluate_cf_plan` rejected 分岐 / `_close_cf_window` latch を修正)

- [ ] **Step 3: Commit**

```bash
git add -f tests/test_watch_counterfactual.py
git commit -m "test: rejectedの直接cf化・invalidation latch・live執行境界の検証 (F-4c)"
```

---

### Task 12: F-4d — finalize 待ち回収 (crash recovery)

**Files:**
- Modify: なし (Task 9 で配線済みの回収ループを検証)
- Test: `tests/test_watch_counterfactual.py` (追記)

- [ ] **Step 1: Write the tests**

```python
# ── Task 12: finalize 待ち回収 (crash recovery) ────────────────


def test_crashed_rejected_finalize_recovered_next_tick(tmp_path):
    """reject 直後 (finalize 前) にクラッシュした状態 → 次 tick で cf 行が揃う。"""
    rt = _make_runtime(tmp_path, mid=150.50)  # entry 非成立の価格 (回収経路のみを検証)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_decide_gate(plan_id, "rejected")
    # ここで crash した想定: rejected + would_trigger + 行なし
    assert rt._orch.get_shadow_trigger(plan_id) is None

    rt.run_watch_cycle(now=NOW)

    plan = rt._orch.get_trade_plan(plan_id)
    assert plan.cf_state == "triggered"
    trig = rt._orch.get_shadow_trigger(plan_id)
    assert trig is not None
    assert trig.trigger_price == 150.25        # stamp 値で記録される
    dec = rt._orch.get_decision(trig.decision_id)
    assert dec.decision_type == "plan_cf_trigger"


def test_crashed_expired_finalize_recovered(tmp_path):
    """expiry 遷移後 (finalize 前) のクラッシュも回収される。"""
    rt = _make_runtime(tmp_path, mid=150.50)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_close_pending_unanswered(plan_id, "expired")
    rt.run_watch_cycle(now=NOW)
    assert rt._orch.get_trade_plan(plan_id).cf_state == "triggered"
    assert rt._orch.get_shadow_trigger(plan_id) is not None


def test_recovery_idempotent_across_ticks(tmp_path):
    rt = _make_runtime(tmp_path, mid=150.50)
    plan_id = _create_gate_plan(rt._orch)
    rt._orch.try_stamp_would_trigger(
        plan_id, at=NOW - timedelta(minutes=10), price=150.25, spread_pips=1.2)
    rt._orch.try_decide_gate(plan_id, "rejected")
    rt.run_watch_cycle(now=NOW)
    rt.run_watch_cycle(now=NOW + timedelta(seconds=1))  # 2 周しても行は増えない
    assert rt._orch.get_shadow_trigger(plan_id) is not None
    assert rt._orch.get_cf_finalize_pending("USDJPY=X") == []
```

- [ ] **Step 2: Run tests**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_watch_counterfactual.py -q'`
Expected: all passed

- [ ] **Step 3: 全体回帰 + Commit**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/ -q -x --timeout=300 2>&1 | tail -5'`
Expected: all passed

```bash
git add -f tests/test_watch_counterfactual.py
git commit -m "test: finalize待ち回収 (crash recovery) の収束・冪等性検証 (F-4d)"
```

---

### Task 13: F-5 store 補助 — reasoning JOIN / gate_message / reconcile query

**Files:**
- Modify: `src/data/orchestrator_store.py` (`get_cf_finalize_pending` の直後)
- Test: `tests/test_gate_store.py` (追記)

- [ ] **Step 1: Write the failing tests**

```python
# ── Task 13: F-5 補助 (reasoning JOIN / gate_message / reconcile) ──


def test_latest_plan_create_reasoning(store):
    plan_id = _plan(store)
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    store.record_decision(
        run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
        decision_type="plan_create", decision="buy", plan_id=plan_id,
        reasoning_summary="古い理由")
    store.record_decision(
        run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
        decision_type="plan_create", decision="buy", plan_id=plan_id,
        reasoning_summary="最新の理由")
    assert store.get_latest_plan_create_reasoning(plan_id) == "最新の理由"


def test_latest_reasoning_none_when_missing(store):
    assert store.get_latest_plan_create_reasoning(_plan(store)) is None


def test_set_gate_message_idempotent(store):
    plan_id = _plan(store)
    assert store.set_gate_message(plan_id, "msg-123") is True
    assert store.set_gate_message(plan_id, "msg-123") is True  # 冪等
    assert store.get_trade_plan(plan_id).gate_message_id == "msg-123"
    assert store.set_gate_message(99999, "msg-x") is False     # 404 相当


def test_gate_posted_plans_window_and_any_status(store):
    p1 = _plan(store)
    store.set_gate_message(p1, "m1")
    store.try_decide_gate(p1, "rejected")     # 非 pending でも返る (status 不問)
    p2 = _plan(store)                          # 未投稿 → 返らない
    p3 = _plan(store)
    store.set_gate_message(p3, "m3")
    rows = store.get_gate_posted_plans(within_hours=24)
    ids = {p.plan_id for p in rows}
    assert ids == {p1, p3}
    assert p2 not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_gate_store.py -q -k "reasoning or gate_message or posted"'`
Expected: FAIL — AttributeError (3 メソッド未定義)

- [ ] **Step 3: Implement**

```python
    def get_latest_plan_create_reasoning(self, plan_id: int) -> str | None:
        """plan の最新 plan_create decision の reasoning_summary を返す (gate spec F-5)。

        trade_plans に reasoning は冗長保存しない — 真実源は decision 側の 1 箇所
        (codex Medium)。pending 一覧 API / plan 詳細がこれを JOIN 相当で使う。
        """
        with Session(self._engine) as session:
            stmt = (
                select(_OrchestratorDecision.reasoning_summary)
                .where(_OrchestratorDecision.plan_id == plan_id)
                .where(_OrchestratorDecision.decision_type == "plan_create")
                .order_by(_OrchestratorDecision.decision_id.desc())
            )
            return session.execute(stmt).scalars().first()

    def set_gate_message(self, plan_id: int, message_id: str) -> bool:
        """Discord message ID を保存する (gate spec F-5 gate_message endpoint)。

        bot が投稿直後に呼ぶ。冪等 (同値・別値とも上書き可)。plan なしは False (404)。
        """
        with Session(self._engine) as session:
            plan = session.get(_TradePlan, plan_id)
            if plan is None:
                return False
            plan.gate_message_id = message_id
            plan.updated_at = db_now()
            session.commit()
            return True

    def get_gate_posted_plans(self, *, within_hours: int) -> list[_TradePlan]:
        """投稿済み (gate_message_id あり) plan を status 不問で返す (F-5 reconcile)。

        bot 再起動時に「停止中に pending から消えた投稿済み plan」のメッセージ edit
        復旧に使う (codex 2巡目 Low-Med)。updated_at 窓で有界。
        """
        cutoff = db_now() - timedelta(hours=within_hours)
        with Session(self._engine) as session:
            stmt = (
                select(_TradePlan)
                .where(_TradePlan.gate_message_id.is_not(None))
                .where(_TradePlan.updated_at >= cutoff)
                .order_by(_TradePlan.created_at.desc())
            )
            plans = list(session.execute(stmt).scalars().all())
            for p in plans:
                session.expunge(p)
            return plans
```

`timedelta` が import 済みか確認 (`from datetime import datetime, timedelta` — 無ければ追記)。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同上
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py tests/test_gate_store.py
git commit -m "feat: reasoning JOIN/gate_message保存/reconcile query のstore補助 (F-5)"
```

---

### Task 14: F-5 — API endpoint 5本 + 注入配線

**Files:**
- Modify: `src/api/_state.py` (`APIState`)
- Modify: `src/api/server.py` (import + include_router + `start_api_server` 引数)
- Create: `src/api/routes/orchestrator.py`
- Modify: `main.py` (line 488 付近の `start_api_server` 呼び出し)
- Modify: `src/orchestrator/plan_view.py` (`plan_to_row` に reasoning)
- Test: `tests/test_api_orchestrator_plans.py` (新規)

- [ ] **Step 1: Write the failing tests**

`tests/test_api_orchestrator_plans.py` を新規作成:

```python
"""/orchestrator/plans 系 endpoint (gate spec F-5) のテスト。

実 OrchestratorStore (tmp_path SQLite) を state に注入し、TestClient で
一覧 / 詳細 / approve / reject / gate_message / reconcile を検証する。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api._state import state
from src.api.server import app
from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "test-key")


@pytest.fixture
def store(tmp_path: Path):
    s = OrchestratorStore(tmp_path / "orch.db")
    state.orchestrator_store = s
    yield s
    state.orchestrator_store = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _pending_plan(store, *, reasoning: str | None = None) -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    plan_id = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status="pending_approval",
    )
    if reasoning:
        store.record_decision(
            run_id=run_id, snapshot_id=snap, pair="USDJPY=X",
            decision_type="plan_create", decision="buy", plan_id=plan_id,
            reasoning_summary=reasoning)
    return plan_id


def test_list_pending_includes_reasoning_and_message_id(store, client):
    plan_id = _pending_plan(store, reasoning="pullback long")
    store.set_gate_message(plan_id, "msg-1")
    res = client.get("/orchestrator/plans", headers=HEADERS)
    assert res.status_code == 200
    rows = res.json()["plans"]
    assert len(rows) == 1
    assert rows[0]["plan_id"] == plan_id
    assert rows[0]["reasoning"] == "pullback long"
    assert rows[0]["gate_message_id"] == "msg-1"
    assert rows[0]["status"] == "pending_approval"


def test_detail_returns_gate_fields(store, client):
    plan_id = _pending_plan(store)
    store.try_decide_gate(plan_id, "rejected", reason="RR悪い")
    res = client.get(f"/orchestrator/plans/{plan_id}", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "rejected"
    assert body["gate_decision"] == "rejected"
    assert body["gate_reason"] == "RR悪い"


def test_detail_404(store, client):
    assert client.get("/orchestrator/plans/999", headers=HEADERS).status_code == 404


def test_approve_then_conflict(store, client):
    plan_id = _pending_plan(store)
    res = client.post(f"/orchestrator/plans/{plan_id}/approve", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "active"
    # 二重決定は 409
    res2 = client.post(f"/orchestrator/plans/{plan_id}/reject", headers=HEADERS)
    assert res2.status_code == 409


def test_reject_persists_reason(store, client):
    plan_id = _pending_plan(store)
    res = client.post(
        f"/orchestrator/plans/{plan_id}/reject",
        headers=HEADERS, json={"reason": "タイミング悪い"})
    assert res.status_code == 200
    assert store.get_trade_plan(plan_id).gate_reason == "タイミング悪い"


def test_gate_message_endpoint(store, client):
    plan_id = _pending_plan(store)
    res = client.post(
        f"/orchestrator/plans/{plan_id}/gate_message",
        headers=HEADERS, json={"message_id": "m-42"})
    assert res.status_code == 200
    assert store.get_trade_plan(plan_id).gate_message_id == "m-42"
    assert client.post(
        "/orchestrator/plans/999/gate_message",
        headers=HEADERS, json={"message_id": "m"}).status_code == 404


def test_reconcile_posted_within_hours(store, client):
    plan_id = _pending_plan(store)
    store.set_gate_message(plan_id, "m-1")
    store.try_decide_gate(plan_id, "rejected")   # pending から消えた投稿済み plan
    res = client.get(
        "/orchestrator/plans?posted_within_hours=24", headers=HEADERS)
    rows = res.json()["plans"]
    assert [r["plan_id"] for r in rows] == [plan_id]
    assert rows[0]["gate_decision"] == "rejected"


def test_auth_required(store, client):
    assert client.get("/orchestrator/plans").status_code in (401, 403, 422)


def test_store_not_configured_returns_503(client):
    state.orchestrator_store = None
    assert client.get("/orchestrator/plans", headers=HEADERS).status_code == 503
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_api_orchestrator_plans.py -q'`
Expected: FAIL — 404 (route 未登録) / AttributeError (state.orchestrator_store 未定義)

- [ ] **Step 3: Implement**

(a) `src/api/_state.py` — `APIState` に追加 (`forecast_store` の直後):

```python
    orchestrator_store: Any = None  # OrchestratorStore (gate spec F-5)
```

(b) `src/orchestrator/plan_view.py` — `plan_to_row` シグネチャと戻り値を拡張:

```python
def plan_to_row(plan: "_TradePlan", *, reasoning: str | None = None) -> dict[str, Any]:
```

戻り dict の末尾に追加:

```python
        "reasoning": reasoning,
```

docstring の「(reasoning は F-5 時に追加)」注記を「reasoning は呼び出し側が
`get_latest_plan_create_reasoning` で引いて渡す (F-5)」に更新。

(c) `src/api/routes/orchestrator.py` を新規作成:

```python
"""orchestrator plan 系 endpoint (approval gate spec F-5)。

権威は finance 側 — discord_bot は UI アダプタとしてここを呼ぶ。
認証は既存 X-API-Key (verify_api_key)。store は APIState.orchestrator_store
(start_api_server で注入。未注入は 503 — headless 構成の保護)。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api._state import state, verify_api_key
from src.orchestrator.plan_view import plan_to_row

router = APIRouter()


def _store():
    store = state.orchestrator_store
    if store is None:
        raise HTTPException(status_code=503, detail="orchestrator store not configured")
    return store


def _row(store, plan) -> dict[str, Any]:
    row = plan_to_row(
        plan, reasoning=store.get_latest_plan_create_reasoning(plan.plan_id)
    )
    row["status"] = plan.status
    row["gate_decision"] = plan.gate_decision
    row["gate_message_id"] = plan.gate_message_id
    return row


@router.get("/orchestrator/plans", dependencies=[Depends(verify_api_key)])
def list_plans(
    status: str = "pending_approval",
    posted_within_hours: int | None = None,
) -> dict[str, Any]:
    """plan 一覧。posted_within_hours 指定時は reconcile モード (status 不問・
    gate_message_id あり・updated_at 窓 — bot 再起動復旧用)。"""
    store = _store()
    if posted_within_hours is not None:
        plans = store.get_gate_posted_plans(within_hours=posted_within_hours)
    else:
        plans = store.get_plans_by_status((status,))
    return {"plans": [_row(store, p) for p in plans]}


@router.get("/orchestrator/plans/{plan_id}", dependencies=[Depends(verify_api_key)])
def plan_detail(plan_id: int) -> dict[str, Any]:
    """plan 詳細 — polling で pending から消えた plan の結末判定 (bot の edit 用)。"""
    store = _store()
    plan = store.get_trade_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    row = _row(store, plan)
    row["gate_decided_at"] = plan.gate_decided_at
    row["gate_reason"] = plan.gate_reason
    return row


class _RejectBody(BaseModel):
    reason: str | None = None


class _GateMessageBody(BaseModel):
    message_id: str


@router.post(
    "/orchestrator/plans/{plan_id}/approve",
    dependencies=[Depends(verify_api_key)],
)
def approve_plan(plan_id: int) -> dict[str, Any]:
    if not _store().try_decide_gate(plan_id, "approved"):
        raise HTTPException(status_code=409, detail="plan is not pending_approval")
    return {"plan_id": plan_id, "status": "active"}


@router.post(
    "/orchestrator/plans/{plan_id}/reject",
    dependencies=[Depends(verify_api_key)],
)
def reject_plan(plan_id: int, body: _RejectBody | None = None) -> dict[str, Any]:
    reason = body.reason if body is not None else None
    if not _store().try_decide_gate(plan_id, "rejected", reason=reason):
        raise HTTPException(status_code=409, detail="plan is not pending_approval")
    return {"plan_id": plan_id, "status": "rejected"}


@router.post(
    "/orchestrator/plans/{plan_id}/gate_message",
    dependencies=[Depends(verify_api_key)],
)
def set_gate_message(plan_id: int, body: _GateMessageBody) -> dict[str, Any]:
    """bot が Discord 投稿直後に呼ぶ (再起動突合の正本を finance 側へ)。冪等。"""
    if not _store().set_gate_message(plan_id, body.message_id):
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan_id": plan_id, "gate_message_id": body.message_id}
```

(d) `src/api/server.py`:
- routes import 行 (`from src.api.routes import ...`) に `orchestrator` を追加
- `app.include_router(ask.router)` の直後に `app.include_router(orchestrator.router)`
- `start_api_server` に引数 `orchestrator_store=None` を追加し、本体で `state.orchestrator_store = orchestrator_store`

```python
def start_api_server(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    llm_slot: PriorityJobSlot,
    price_store: PriceStore,
    hold_store,        # HoldDecisionStore
    forecast_store,    # ForecastStore
    orchestrator_store=None,  # OrchestratorStore (gate spec F-5)
) -> threading.Thread:
```

(e) `main.py` line 488 付近:

```python
    if config.api.enabled:
        from src.api.server import start_api_server
        from src.data.orchestrator_store import OrchestratorStore
        # gate spec F-5: API から plan gate を操作するための store。engine は
        # _get_engine が db_path 単位で共有するため runtime 側と実体は同一。
        start_api_server(config, store, analysis_store, _llm_slot,
                         price_store, hold_store, forecast_store,
                         OrchestratorStore(config.prices_db_path))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_api_orchestrator_plans.py tests/test_api_endpoints.py tests/test_cli_plans.py tests/test_plan_view.py -q'`
Expected: all passed (plan_to_row 変更の CLI 回帰含む)

- [ ] **Step 5: Commit**

```bash
git add -f src/api/_state.py src/api/server.py src/api/routes/orchestrator.py main.py src/orchestrator/plan_view.py tests/test_api_orchestrator_plans.py
git commit -m "feat: /orchestrator/plans API 5本 + APIState注入 — 承認ゲートのUI境界 (F-5)"
```

---

### Task 15: F-7 — metrics の gate 対応 (real 集計フィルタ + label 集計 + summary)

**Files:**
- Modify: `src/data/orchestrator_store.py` (`get_shadow_metrics_raw`、line 1168 付近)
- Modify: `src/orchestrator/shadow_metrics.py` (`ShadowMetrics` + `compute_shadow_metrics`)
- Modify: `src/orchestrator/shadow_notifier.py` (`notify_daily_summary`、line 168 付近)
- Test: `tests/test_shadow_metrics.py` (追記)

- [ ] **Step 1: Write the failing tests**

`tests/test_shadow_metrics.py` に追記 (**注**: 同ファイル既存の seed helper があればそれを使い、無ければ以下の `_gate_plan` を追加する):

```python
# ── gate spec F-7: real/cf 分離 + label 集計 ──────────────────

from datetime import timedelta

from src.utils.clock import db_now


def _gate_plan(store, *, status="pending_approval") -> int:
    snap = store.create_snapshot(pair="USDJPY=X", as_of_time=db_now())
    run_id = store.start_run("PlannerAgent", pair="USDJPY=X")
    return store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=snap, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.4, "tp": 151.5, "rr": 2.0}, invalidation_json=[],
        expires_at=db_now() + timedelta(hours=8), created_by_run_id=run_id,
        status=status,
    )


def test_real_plan_counts_exclude_cf_labels(tmp_path):
    """rejected/unanswered は実性能 plan_counts から除外。gate OFF (NULL) は含む
    (NOT IN の NULL 罠 — codex 2巡目 Medium の回帰テスト)。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    _gate_plan(store, status="active")           # gate OFF 相当 (gate_decision NULL)
    p_rej = _gate_plan(store)
    store.try_decide_gate(p_rej, "rejected")
    p_un = _gate_plan(store)
    store.try_close_pending_unanswered(p_un, "expired")
    p_appr = _gate_plan(store)
    store.try_decide_gate(p_appr, "approved")    # approved は real 側に含む

    raw = store.get_shadow_metrics_raw()
    # active 2 件 (NULL + approved)。rejected/expired(unanswered) は数えない
    assert raw["plan_counts"].get("active") == 2
    assert "rejected" not in raw["plan_counts"]
    assert raw["plan_counts"].get("expired") is None


def test_cf_hindsight_excluded_from_real_aggregates(tmp_path):
    """cf 行の hindsight は実性能 avg_pnl_r に混入しない。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    p_rej = _gate_plan(store)
    store.try_decide_gate(p_rej, "rejected")
    trig_id = store.finalize_cf_trigger(
        p_rej, decision_id=None, enqueue_hindsight=True, horizon_seconds=60,
        triggered_at=db_now() - timedelta(hours=2), trigger_price=150.0,
        spread_pips=1.0)
    ev = store.get_pending_hindsight_evaluations(now=db_now())[0]
    store.update_hindsight_evaluation(
        ev.id, status="evaluated", evaluated_at=db_now(), pnl_r=5.0)
    raw = store.get_shadow_metrics_raw()
    assert raw["avg_pnl_r"] is None              # real 側は空 (cf の 5.0 が混ざらない)
    assert raw["gate_avg_pnl_r"].get("rejected") == 5.0


def test_gate_label_counts(tmp_path):
    store = OrchestratorStore(tmp_path / "orch.db")
    p1 = _gate_plan(store); store.try_decide_gate(p1, "approved")
    p2 = _gate_plan(store); store.try_decide_gate(p2, "rejected")
    p3 = _gate_plan(store); store.try_close_pending_unanswered(p3, "expired")
    raw = store.get_shadow_metrics_raw()
    assert raw["gate_plan_counts"] == {"approved": 1, "rejected": 1, "unanswered": 1}


def test_compute_metrics_gate_labels_shape(tmp_path):
    from src.orchestrator.shadow_metrics import compute_shadow_metrics
    store = OrchestratorStore(tmp_path / "orch.db")
    p = _gate_plan(store); store.try_decide_gate(p, "rejected")
    m = compute_shadow_metrics(store, now=db_now())
    assert m.gate_labels["rejected"]["plans"] == 1
    assert "triggers" in m.gate_labels["rejected"]
    assert "avg_pnl_r" in m.gate_labels["rejected"]
```

**注**: `update_hindsight_evaluation` のシグネチャ (pnl_r を kwargs で受けるか) は
`grep -n "def update_hindsight_evaluation" -A 15 src/data/orchestrator_store.py` で確認し、
テストの呼び方を実シグネチャに合わせる。

- [ ] **Step 2: Run tests to verify they fail**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_shadow_metrics.py -q -k "real_plan or cf_hindsight or gate_label or gate_labels"'`
Expected: FAIL — plan_counts に rejected が混入 / KeyError: gate_plan_counts

- [ ] **Step 3: Implement**

(a) `get_shadow_metrics_raw` — メソッド冒頭 (Session 内の最初) に real フィルタを定義し、plan_stmt と hindsight 系に適用。gate label 集計を追加:

```python
            # gate spec F-7: 実性能集計から反実仮想 plan (rejected/unanswered) を除外。
            # SQL の NOT IN は NULL を落とすため IS NULL OR が必須 — gate OFF plan
            # (gate_decision NULL) が集計から消える (codex 2巡目 Medium)。
            _real = or_(
                _TradePlan.gate_decision.is_(None),
                _TradePlan.gate_decision.notin_(("rejected", "unanswered")),
            )
```

plan_stmt に `.where(_real)` を追加:

```python
            plan_stmt = (
                select(_TradePlan.status, func.count())
                .where(_real)
                .group_by(_TradePlan.status)
            )
```

hs_stmt / evaluated_stmt は **常に** triggers→plans を join して `_real` を適用する
(従来は trade_horizon 指定時のみ join)。既存の horizon join ブロックを次の形に統合:

```python
            hs_stmt = (
                select(_ShadowHindsightEvaluation.status, func.count())
                .join(
                    _ShadowTrigger,
                    _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                )
                .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                .where(_real)
            )
            evaluated_stmt = (
                select(
                    func.avg(_ShadowHindsightEvaluation.mfe_r),
                    func.avg(_ShadowHindsightEvaluation.mae_r),
                    func.avg(_ShadowHindsightEvaluation.pnl_r),
                    func.sum(_ShadowHindsightEvaluation.would_hit_sl),
                    func.sum(_ShadowHindsightEvaluation.would_hit_tp),
                )
                .select_from(_ShadowHindsightEvaluation)
                .join(
                    _ShadowTrigger,
                    _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                )
                .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                .where(_ShadowHindsightEvaluation.status == "evaluated")
                .where(_real)
            )
            if trade_horizon is not None:
                hs_stmt = hs_stmt.where(_TradePlan.horizon == trade_horizon)
                evaluated_stmt = evaluated_stmt.where(_TradePlan.horizon == trade_horizon)
```

freshness の後・return の前に gate label 集計を追加:

```python
            # gate label 別集計 (F-7): plan 件数 / trigger 行数 / hindsight 平均 pnl_r。
            # approved の行は real、rejected/unanswered の行は cf — plan 1件=行1本
            # (UNIQUE) なので gate_decision の JOIN だけで排反に分離できる。
            gate_plan_counts = dict(session.execute(
                select(_TradePlan.gate_decision, func.count())
                .where(_TradePlan.gate_decision.is_not(None))
                .group_by(_TradePlan.gate_decision)
            ).all())
            gate_trigger_counts = dict(session.execute(
                select(_TradePlan.gate_decision, func.count())
                .select_from(_ShadowTrigger)
                .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                .where(_TradePlan.gate_decision.is_not(None))
                .group_by(_TradePlan.gate_decision)
            ).all())
            gate_avg_pnl_r = dict(session.execute(
                select(
                    _TradePlan.gate_decision,
                    func.avg(_ShadowHindsightEvaluation.pnl_r),
                )
                .select_from(_ShadowHindsightEvaluation)
                .join(
                    _ShadowTrigger,
                    _ShadowTrigger.id == _ShadowHindsightEvaluation.shadow_trigger_id,
                )
                .join(_TradePlan, _TradePlan.plan_id == _ShadowTrigger.plan_id)
                .where(_ShadowHindsightEvaluation.status == "evaluated")
                .where(_TradePlan.gate_decision.is_not(None))
                .group_by(_TradePlan.gate_decision)
            ).all())
```

return dict に追加:

```python
            "gate_plan_counts": gate_plan_counts,
            "gate_trigger_counts": gate_trigger_counts,
            "gate_avg_pnl_r": gate_avg_pnl_r,
```

(b) `src/orchestrator/shadow_metrics.py`:

`dataclass` import 行を `from dataclasses import dataclass, field` に変更。`ShadowMetrics` 末尾に:

```python
    # 6. approval gate (spec F-7): label 別 {plans, triggers, avg_pnl_r}。
    # approved=real 成績 / rejected・unanswered=反実仮想成績。空 dict = gate 未使用。
    gate_labels: dict = field(default_factory=dict)
```

`compute_shadow_metrics` の return 直前に:

```python
    gate_labels = {
        label: {
            "plans": count,
            "triggers": raw["gate_trigger_counts"].get(label, 0),
            "avg_pnl_r": raw["gate_avg_pnl_r"].get(label),
        }
        for label, count in raw["gate_plan_counts"].items()
    }
```

return に `gate_labels=gate_labels,` を追加。

(c) `src/orchestrator/shadow_notifier.py` `notify_daily_summary` — `lines` リストの
`freshness blocks` 行の後に:

```python
        if metrics.gate_labels:
            parts = []
            for label in ("approved", "rejected", "unanswered"):
                g = metrics.gate_labels.get(label)
                if g:
                    parts.append(
                        f"{label}={g['plans']}p/{g['triggers']}t/"
                        f"{_fmt_opt(g['avg_pnl_r'])}R"
                    )
            if parts:
                lines.append("gate: " + " | ".join(parts))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_shadow_metrics.py tests/test_shadow_metrics_horizon.py tests/test_runtime_daily_summary.py -q'`
Expected: all passed。**join 化で既存 hindsight テストが落ちた場合**: seed が trigger/plan なしの孤児 hindsight 行を作っていないか確認し、正規の連鎖 (plan→trigger→hindsight) で seed し直す (spec 上 hindsight は必ず trigger 起点)。

- [ ] **Step 5: Commit**

```bash
git add -f src/data/orchestrator_store.py src/orchestrator/shadow_metrics.py src/orchestrator/shadow_notifier.py tests/test_shadow_metrics.py
git commit -m "feat: metrics のreal/cf分離 (NULL-safe) + gate label集計 + daily summary行 (F-7)"
```

---

### Task 16: 全体回帰 + 実機確認

- [ ] **Step 1: Full suite**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5'`
Expected: all passed (1400+ 想定)

- [ ] **Step 2: 実 config での起動経路確認 (import/配線ミス検出)**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -c "
from src.config import load_config
from src.data.orchestrator_store import OrchestratorStore
from src.api.server import app  # route 登録の import エラー検出
cfg = load_config()
print(\"approval_gate =\", cfg.orchestrator.approval_gate)
s = OrchestratorStore(cfg.prices_db_path)   # 実 DB への idempotent migration
print(\"pending =\", len(s.get_plans_by_status((\"pending_approval\",))))
print(\"OK\")"'`
Expected: `approval_gate = False` / `pending = 0` / `OK` (既定 OFF = 挙動不変の確認)

- [ ] **Step 3: CLI plans の表示確認 (gate と同じ経路)**

Run: `wsl -d Ubuntu-24.04 bash -lc 'cd /home/teru/project/finance && .venv/bin/python -c "
from src.config import load_config
from src.cli import _cmd_plans
_cmd_plans(load_config())"'`
Expected: 承認待ち/発注監視中の 2 セクションが例外なく表示される

- [ ] **Step 4: Commit (残があれば) + 完了報告**

働き残しの変更が無いことを `git status --short` で確認。

---

## 実装後の運用メモ (プラン外)

- gate ON は `config/settings.yaml` (gitignore) に `orchestrator.approval_gate: true` を足して再起動 — paper で即ラベル蓄積開始 (spec §5-4)。
- 稼働デーモンへの反映は再起動時。Fiosracht へは rsync 安全手順厳守 ([[finance_rsync_safety]])。
- 次プラン: discord_bot 側 (FinanceCog polling + persistent view + FinanceClient 5 メソッド — spec §4)。
