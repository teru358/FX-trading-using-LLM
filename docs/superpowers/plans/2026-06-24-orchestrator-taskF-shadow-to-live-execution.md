# Orchestrator Task F — shadow→本番発注 昇格 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** orchestrator の watch trigger を本番発注に結線し、`OrchestratorConfig.mode=shadow→live` で shadow→本番発注を昇格できるようにする (broker 結線 + durable order lock + クラッシュ復旧 + single execution writer)。

**Architecture:** live mode のとき `_record_shadow_trigger` 成功直後に同一 watch スレッド内で `_execute_live_trigger` を同期実行 (案A: claim-first → final gate → submit-marked → execute → 結果反映)。order_intents (plan_id UNIQUE) で二重発注を durable に防ぎ、起動時 recovery job がクラッシュ pending を 3 分岐で処理。旧 trading cycle の entry phase は `OrchestratorConfig.mode=live` 時に全 entry point で停止 (single execution writer)。

**Tech Stack:** Python 3, SQLAlchemy ORM (orchestrator_store), 既存 `BrokerAdapter`/`RiskGateWorker`/`TradeSignal`/`ExecutionPlanDraft`, pytest。

**Spec:** `docs/superpowers/specs/2026-06-23-orchestrator-taskF-shadow-to-live-execution-design.md`

---

## File Structure

**変更:**
- `src/config/schema.py` — `OrchestratorConfig.mode` validation (shadow/live)、`execution_opinion_recheck_enabled`、`AppConfig` の cross-field validation (orchestrator.mode=live → AppConfig.mode=live + live_broker)。
- `src/data/orchestrator_store.py` — recovery query 拡張 (`get_stale_or_unconfirmed_intents`)、`set_recovery_status`。
- `src/orchestrator/runtime.py` — `_execute_live_trigger` (執行段)、`_record_shadow_trigger` の live 分岐、`__init__` に `execution_broker`/`execution_position_mgr`/`mode` 受け、reject 後の plan/intent 遷移、起動時 recovery job。
- `src/orchestrator/bootstrap.py` — live 時に execution broker + position_mgr 構築 + runtime 注入、recovery job 起動。
- `src/trading/order_intent_status.py` (新規) — `EXECUTION_OUTCOME_TO_INTENT_STATUS` mapping (single source of truth)。
- `src/cycles/trading.py` — entry phase の統一ガード (orchestrator.mode=live で entry skip)。

**新規テスト:**
- `tests/test_taskf_order_intent_status_map.py`
- `tests/test_taskf_config_validation.py`
- `tests/test_taskf_recovery_query.py`
- `tests/test_taskf_execute_live_trigger.py`
- `tests/test_taskf_reject_transitions.py`
- `tests/test_taskf_recovery_job.py`
- `tests/test_taskf_single_writer_guard.py`
- `tests/test_taskf_bootstrap_wiring.py`

**実行環境 (全タスク共通):**
- テストは WSL venv で実行: `wsl.exe -d Ubuntu-24.04 -- bash -lc 'cd ~/project/finance && source .venv/bin/activate && python -m pytest <args>'`
- git は WSL パス: `cd "//wsl.localhost/Ubuntu-24.04/home/teru/project/finance" && git ...`。branch `feat/planner-watch-loop`、attribution trailer なし。
- 不変条件: `OrchestratorConfig.mode=shadow` (既定) で全既存挙動が無改変 (回帰グリーン)。実 broker/LLM はテストで mock (本番発注を起こさない)。

---

## Task 1: `ExecutionResult.outcome → order_intent.status` mapping (codex #2)

**Files:**
- Create: `src/trading/order_intent_status.py`
- Test: `tests/test_taskf_order_intent_status_map.py`

**Note:** 既存 `ORDER_INTENT_STATUSES = pending/submitted/filled/rejected/failed/needs_reconcile/abandoned` (`orchestrator_store.py:105`)。`ExecutionResult.outcome` は `executed/skipped/halted/rejected/failed` (`broker_adapter.py:10`)。spec §2 step6 の mapping を単一 source of truth として切り出す。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_order_intent_status_map.py` を新規作成:

```python
import pytest

from src.trading.order_intent_status import (
    EXECUTION_OUTCOME_TO_INTENT_STATUS,
    intent_status_for_outcome,
    is_alertable_outcome,
)
from src.data.orchestrator_store import ORDER_INTENT_STATUSES


def test_mapping_covers_all_execution_outcomes():
    for outcome in ("executed", "skipped", "halted", "rejected", "failed"):
        assert outcome in EXECUTION_OUTCOME_TO_INTENT_STATUS


def test_mapped_statuses_are_valid_enum_values():
    for status in EXECUTION_OUTCOME_TO_INTENT_STATUS.values():
        assert status in ORDER_INTENT_STATUSES


def test_executed_maps_to_filled():
    assert intent_status_for_outcome("executed") == "filled"


def test_skipped_maps_to_abandoned():
    assert intent_status_for_outcome("skipped") == "abandoned"


def test_halted_and_rejected_map_to_rejected():
    assert intent_status_for_outcome("halted") == "rejected"
    assert intent_status_for_outcome("rejected") == "rejected"


def test_failed_maps_to_failed():
    assert intent_status_for_outcome("failed") == "failed"


def test_unknown_outcome_raises():
    with pytest.raises(KeyError):
        intent_status_for_outcome("bogus")


def test_alertable_outcomes():
    # executed / skipped は想定内 (alert なし)、halted/rejected/failed は要注意
    assert is_alertable_outcome("executed") is False
    assert is_alertable_outcome("skipped") is False
    assert is_alertable_outcome("halted") is True
    assert is_alertable_outcome("rejected") is True
    assert is_alertable_outcome("failed") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_order_intent_status_map.py -v`
Expected: FAIL — `src.trading.order_intent_status` が無い (ModuleNotFoundError)。

- [ ] **Step 3: Write minimal implementation**

`src/trading/order_intent_status.py` を新規作成:

```python
"""ExecutionResult.outcome → order_intents.status の単一 mapping (spec §2 step6, Task F)。

broker の ExecutionResult.outcome (executed/skipped/halted/rejected/failed) を
order_intents の status enum (ORDER_INTENT_STATUSES) に明示変換する。enum 外を
record_order_result に渡すと ValueError になるため、ここで一元管理する。
"""
from __future__ import annotations

# spec §2 step6 の mapping 表。
EXECUTION_OUTCOME_TO_INTENT_STATUS: dict[str, str] = {
    "executed": "filled",      # 約定
    "skipped": "abandoned",    # 想定内抑制 (既存ポジ/hold/リスク制限) — plan は用済み
    "rejected": "rejected",    # gate/broker 拒否
    "halted": "rejected",      # halt 状態 → reject と同じく発注見送り
    "failed": "failed",        # 技術的失敗 (bridge 不通等)
}

# 想定内 (alert 不要) の outcome。それ以外は要注意通知。
_NON_ALERT_OUTCOMES = frozenset({"executed", "skipped"})


def intent_status_for_outcome(outcome: str) -> str:
    """outcome を order_intents.status へ変換する。未知 outcome は KeyError。"""
    return EXECUTION_OUTCOME_TO_INTENT_STATUS[outcome]


def is_alertable_outcome(outcome: str) -> bool:
    """要注意通知が必要な outcome か (halted/rejected/failed=True)。"""
    return outcome not in _NON_ALERT_OUTCOMES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_order_intent_status_map.py -v`
Expected: PASS (8 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/trading/order_intent_status.py tests/test_taskf_order_intent_status_map.py
git commit -m "feat: ExecutionResult.outcome → order_intent.status mapping (Task F, codex #2)"
```

---

## Task 2: config validation — `OrchestratorConfig.mode` + AppConfig cross-field (codex 追加確認)

**Files:**
- Modify: `src/config/schema.py` (`OrchestratorConfig.__post_init__` に mode validation + `execution_opinion_recheck_enabled` フィールド、`AppConfig.__post_init__` に cross-field validation)
- Test: `tests/test_taskf_config_validation.py`

**Note:** `OrchestratorConfig.mode: str = "shadow"` は既存 (`schema.py:678`)。トップレベル `AppConfig.mode: str = "paper"` (`schema.py:727`) と別物。発注 broker は AppConfig.mode + live_broker で選ばれる。`OrchestratorConfig` 単体では AppConfig を参照できないので cross-field 検証は `AppConfig.__post_init__` に置く。`AppConfig` の live_broker フィールド名は実装時に確認 (`schema.py` の `live_broker` / `trading.live_broker` のいずれか — 既存 `create_broker` 呼出箇所 `src/cycles/trading.py:1026` 周辺で確認して合わせる)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_config_validation.py` を新規作成:

```python
import pytest

from src.config.schema import OrchestratorConfig


def test_orchestrator_mode_defaults_shadow():
    cfg = OrchestratorConfig()
    assert cfg.mode == "shadow"
    assert cfg.execution_opinion_recheck_enabled is False


def test_orchestrator_mode_accepts_shadow_and_live():
    for m in ("shadow", "live"):
        assert OrchestratorConfig(mode=m).mode == m


def test_orchestrator_mode_rejects_unknown():
    with pytest.raises(ValueError):
        OrchestratorConfig(mode="bogus")
```

> AppConfig の cross-field validation のテストは Step 6 で追加 (AppConfig 構築は重いので最小 fixture を用意する。既存 `tests/test_config_loader.py` の AppConfig 構築 helper があればそれを使い、無ければ loader 経由で最小 dict から構築する)。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_config_validation.py -v`
Expected: FAIL — `execution_opinion_recheck_enabled` が無い / `mode="bogus"` が通る。

- [ ] **Step 3: Write minimal implementation (OrchestratorConfig)**

`src/config/schema.py` の `OrchestratorConfig` dataclass にフィールド追加 (既存 `mode` の近く):

```python
    # Task F: material change 時の ExecutionOpinionAgent 再点火 (発注直前)。既定 OFF
    # (まず決定的・高速執行で live 検証、spec §2 step2)。
    execution_opinion_recheck_enabled: bool = False
```

`OrchestratorConfig.__post_init__` (既存。tick_migration_stage validation がある) の末尾に mode validation を追記:

```python
        valid_modes = ("shadow", "live")
        if self.mode not in valid_modes:
            raise ValueError(
                f"OrchestratorConfig.mode must be one of {valid_modes}, got {self.mode!r}"
            )
```

> `observe` は現状未使用。F は shadow/live のみ許容 (spec §5)。既存テストが `mode="observe"` を構築していないか確認 (構築していれば許容値に含めるか別途相談 — 既定 shadow なので通常は無い)。

- [ ] **Step 4: Run test to verify it passes (OrchestratorConfig 分)**

Run: `pytest tests/test_taskf_config_validation.py -v`
Expected: PASS (3 passed)。

- [ ] **Step 5: Write minimal implementation (AppConfig cross-field)**

`src/config/schema.py` の `AppConfig.__post_init__` (無ければ新設、あれば末尾追記) に cross-field validation を追加:

```python
        # Task F: orchestrator が本番発注 (mode=live) するなら、発注 broker を選ぶ
        # top-level mode も live で live_broker 必須 (spec §5 cross-field)。
        # 不一致だと「発注すると言いながら paper broker を握る」矛盾になる。
        if getattr(self.orchestrator, "mode", "shadow") == "live":
            if self.mode != "live":
                raise ValueError(
                    "orchestrator.mode=live requires AppConfig.mode=live "
                    f"(got AppConfig.mode={self.mode!r})"
                )
            if not self._has_live_broker():
                raise ValueError(
                    "orchestrator.mode=live requires a configured live_broker"
                )
```

`_has_live_broker()` は実装時に既存の live_broker 設定の在り処に合わせる (例: `self.live_broker is not None` または `self.trading.live_broker`)。`create_broker` の `live_broker` 引数の出所 (`src/cycles/trading.py:1026` の呼出) を確認して同じ参照にする。

- [ ] **Step 6: Write the cross-field test + run**

`tests/test_taskf_config_validation.py` に追記 (AppConfig 構築は最小 fixture / loader 経由。実装時に既存 AppConfig テストの構築方法に倣う):

```python
def test_appconfig_orchestrator_live_requires_top_level_live():
    """orchestrator.mode=live + AppConfig.mode=paper は ValueError (codex 追加確認)。"""
    # 実装時: 既存 AppConfig 構築 helper / loader で最小 config を作り、
    # orchestrator.mode="live", top-level mode="paper" で ValueError を確認する。
    # (具体的構築は既存 tests/test_config_loader.py のパターンを流用)
    ...
```

> このテストは AppConfig の最小構築が必要。既存テストの構築パターンを確認し、`orchestrator.mode="live"` かつ top-level `mode="paper"` で `pytest.raises(ValueError)`、`mode="live"`+live_broker 設定済で成功、を検証する。AppConfig 構築が重く mock が必要なら、最小限の loader 入力 dict で代替してよい。

Run: `pytest tests/test_taskf_config_validation.py -v`
Expected: PASS。

- [ ] **Step 7: Regression**

Run: `pytest tests/test_orchestrator_config_tick_stage.py tests/test_config_loader.py -q`
Expected: PASS (既存 config テストが壊れていない)。

- [ ] **Step 8: Commit**

```bash
git add src/config/schema.py tests/test_taskf_config_validation.py
git commit -m "feat: OrchestratorConfig.mode validation + AppConfig cross-field (Task F, codex 追加確認)"
```

---

## Task 3: recovery query 拡張 + `set_recovery_status` (codex #1)

**Files:**
- Modify: `src/data/orchestrator_store.py` (新メソッド `get_stale_or_unconfirmed_intents` + `set_recovery_status`)
- Test: `tests/test_taskf_recovery_query.py`

**Note (codex #1):** `mark_order_submitted` は `submitted_at` を埋めると同時に `status="submitted"` にする (`orchestrator_store.py:613-614`)。だが既存 `get_stale_pending_intents` は `status=="pending"` のみ拾う (`:597`)。よって最重要の「送信直後クラッシュ」(`status=submitted` かつ `order_id is null`) が拾われない。新メソッドで `status=="pending"` OR (`status=="submitted"` AND `order_id is null`) を lease 超過で拾う。既存 `get_stale_pending_intents` は他から参照されている可能性があるので**変更せず新メソッドを追加**する (実装時に呼出元を grep 確認)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_recovery_query.py` を新規作成:

```python
from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


def _insert(orch, *, plan_id, lease_offset_sec):
    now = db_now()
    orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=now + timedelta(seconds=lease_offset_sec),
    )


def test_picks_stale_pending(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=1, lease_offset_sec=-60)  # lease 超過 pending
    rows = orch.get_stale_or_unconfirmed_intents(now=db_now())
    assert [r.plan_id for r in rows] == [1]


def test_picks_submitted_without_order_id(tmp_path: Path):
    """送信直後クラッシュ (status=submitted, order_id null) を拾う (codex #1 回帰)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=2, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=2, submitted_at=now)  # status→submitted, order_id まだ null
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert [r.plan_id for r in rows] == [2]


def test_skips_submitted_with_order_id(tmp_path: Path):
    """order_id 付き submitted (正常約定) は recovery 対象外。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=3, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=3, submitted_at=now)
    orch.record_order_result(plan_id=3, status="filled", order_id="mt5:111")
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert rows == []


def test_skips_non_expired_lease(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=4, lease_offset_sec=+300)  # lease まだ有効
    rows = orch.get_stale_or_unconfirmed_intents(now=db_now())
    assert rows == []


def test_set_recovery_status(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _insert(orch, plan_id=5, lease_offset_sec=-60)
    orch.set_recovery_status(plan_id=5, recovery_status="needs_reconcile")
    intent = orch.get_order_intent(plan_id=5)
    assert intent.recovery_status == "needs_reconcile"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_recovery_query.py -v`
Expected: FAIL — `get_stale_or_unconfirmed_intents` / `set_recovery_status` が無い。

- [ ] **Step 3: Write minimal implementation**

`src/data/orchestrator_store.py` の `get_stale_pending_intents` の下に追加 (`select`/`Session`/`or_` を import 済か確認、`or_` が無ければ `from sqlalchemy import or_` 追加):

```python
    def get_stale_or_unconfirmed_intents(self, *, now: datetime) -> list["_OrderIntent"]:
        """recovery 候補を返す (Task F, codex #1)。lease 超過のうち未完了:
        status=="pending" (未送信) OR (status=="submitted" AND order_id is null)
        (送信直後クラッシュ=建玉不明)。後者を拾わないと needs_reconcile を取りこぼす。
        """
        from sqlalchemy import or_

        with Session(self._engine) as session:
            stmt = (
                select(_OrderIntent)
                .where(_OrderIntent.lease_until < now)
                .where(
                    or_(
                        _OrderIntent.status == "pending",
                        (_OrderIntent.status == "submitted")
                        & (_OrderIntent.order_id.is_(None)),
                    )
                )
            )
            rows = list(session.execute(stmt).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    def set_recovery_status(self, *, plan_id: int, recovery_status: str) -> None:
        """recovery job が判定した recovery_status を書き込む (Task F)。"""
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.plan_id == plan_id)
            intent = session.execute(stmt).scalars().first()
            if intent is None:
                logger.warning(f"set_recovery_status: plan_id {plan_id} not found")
                return
            intent.recovery_status = recovery_status
            intent.updated_at = db_now()
            session.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_recovery_query.py -v`
Expected: PASS (5 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/data/orchestrator_store.py tests/test_taskf_recovery_query.py
git commit -m "feat: recovery query で送信直後クラッシュを拾う + set_recovery_status (Task F, codex #1)"
```

---

## Task 4: `_execute_live_trigger` — 執行段 (F-1 / F-3)

**Files:**
- Modify: `src/orchestrator/runtime.py` (`__init__` に `mode`/`execution_broker`/`execution_position_mgr` 追加、`_execute_live_trigger` 新規、`_record_shadow_trigger` の live 分岐)
- Test: `tests/test_taskf_execute_live_trigger.py`

**Note:** 既存 `OrchestratorRuntime.__init__` は `risk_gate` を持つ (`runtime.py:71` `self._risk_gate`)。`_record_shadow_trigger` は shadow_trigger 記録後 `_notify_shadow_trigger` を呼んで終わる (`runtime.py:626`)。F はその直前 (ok 確定後・通知前) に live 分岐を挿す。`RiskGateWorker.pre_check(draft, context) -> RiskGateResult(passed, reject_class, issues)` (`risk_gate.py:83`、reject_class は "structural"=恒久 / "fixable"=一時)。`try_insert_order_intent(*, plan_id, pair, intended_action, owner_run_id, lease_until, decision_id=None, trigger_id=None) -> bool` (`orchestrator_store.py:527`)。`broker.execute_signal(signal, position_mgr, macro_context="") -> ExecutionResult` (`broker_adapter.py:66`)。`TradeSignal(pair, action, confidence, entry_price, stop_loss, take_profit, ...)` (`signal_combiner.py:21`)。`record_order_result(*, plan_id, status, order_id=None, broker_result_json=None)`。`mark_order_submitted(*, plan_id, submitted_at)`。

**重要 (この task の範囲):** F-1 の正常系 (claim→gate pass→submit→execute→filled 反映) と gate reject の発注抑止まで。reject 後の plan/intent 状態遷移は **Task 5** で詳細化 (この task では「reject なら発注しない + intent に rejected」まで、plan 遷移は Task 5)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_execute_live_trigger.py` を新規作成:

```python
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.trading.broker_adapter import ExecutionResult
from src.trading.position_manager import Order


class _FakeBroker:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute_signal(self, signal, position_mgr, macro_context=""):
        self.calls.append(signal)
        return self._result


class _GatePass:
    def pre_check(self, draft, context):
        from src.orchestrator.risk_gate import RiskGateResult
        return RiskGateResult(passed=True)


class _GateReject:
    def __init__(self, reject_class):
        self._rc = reject_class

    def pre_check(self, draft, context):
        from src.orchestrator.risk_gate import RiskGateResult
        return RiskGateResult(passed=False, reject_class=self._rc, issues=["x"])


def _executed_order():
    return Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1.0,
    )


def _runtime_with_execution(tmp_path, broker, risk_gate):
    """live 執行段だけを検証する最小 runtime を組む。

    実装時: 既存 tests/test_orchestrator_runtime.py の runtime 構築を流用し、
    mode="live", execution_broker=broker, execution_position_mgr=stub, risk_gate=risk_gate
    を渡す。orch_store は OrchestratorStore(tmp_path/"orch.db")。
    """
    ...


def test_live_trigger_executes_and_records_filled(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = _runtime_with_execution(tmp_path, broker, _GatePass())
    # plan を 1 件作り active にし run_watch_cycle で entry 成立させる
    # (実装時: 既存テストの plan 投入 helper を使う)。
    plan_id = rt._seed_active_plan_ready_to_trigger()  # ローカル helper
    rt.run_watch_cycle()
    assert len(broker.calls) == 1                    # 発注 1 回
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "filled"
    assert intent.order_id is not None


def test_live_trigger_structural_reject_does_not_execute(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = _runtime_with_execution(tmp_path, broker, _GateReject("structural"))
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert broker.calls == []                        # gate reject → 発注なし
    intent = rt._orch.get_order_intent(plan_id)
    assert intent.status == "rejected"


def test_live_trigger_duplicate_intent_aborts(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    rt = _runtime_with_execution(tmp_path, broker, _GatePass())
    plan_id = rt._seed_active_plan_ready_to_trigger()
    # 既に同 plan_id の intent が存在 (前回発注) → UNIQUE で中止
    rt._orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=99, lease_until=__import__("src.utils.clock", fromlist=["db_now"]).db_now(),
    )
    rt.run_watch_cycle()
    assert broker.calls == []                        # 既発注 → 二重発注しない


def test_shadow_mode_never_executes(tmp_path):
    broker = _FakeBroker(ExecutionResult.executed(_executed_order()))
    # mode="shadow" (既定) では execution_broker を渡しても live 分岐に入らない
    rt = _runtime_with_execution(tmp_path, broker, _GatePass())
    rt._mode = "shadow"  # or 構築時に shadow
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert broker.calls == []                        # shadow 境界維持
```

> `_runtime_with_execution` と `_seed_active_plan_ready_to_trigger` は実装時に既存 `tests/test_orchestrator_runtime.py` / `test_orchestrator_e2e.py` の runtime 構築・plan 投入パターンを流用したローカル helper。entry 成立条件 (quote が entry を満たす) の作り方は既存 watch テストに倣う。mock broker で本番発注は起きない。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_execute_live_trigger.py -v`
Expected: FAIL — runtime が `execution_broker`/`mode` を受けない / `_execute_live_trigger` 未定義。

- [ ] **Step 3: Write minimal implementation (runtime __init__)**

`src/orchestrator/runtime.py` の `OrchestratorRuntime.__init__` に引数追加 (既存 kwargs の並びに合わせる)、属性保持:

```python
        mode: str = "shadow",
        execution_broker=None,
        execution_position_mgr=None,
```
```python
        # Task F: 本番発注 (mode=live) の執行段。shadow では None で live 分岐に入らない。
        self._mode = mode
        self._execution_broker = execution_broker
        self._execution_position_mgr = execution_position_mgr
```

- [ ] **Step 4: Write minimal implementation (_execute_live_trigger + live 分岐)**

`src/orchestrator/runtime.py` の `_record_shadow_trigger` の通知直前 (`if not ok: return False` の後、`self._notify_shadow_trigger(...)` の前) に live 分岐を挿入:

```python
        # Task F: live mode かつ execution broker 注入時のみ本番発注へ進む (single writer)。
        if self._mode == "live" and self._execution_broker is not None:
            self._execute_live_trigger(plan, pair, quote, decision_id, run_id)
```

`_record_shadow_trigger` の下に新メソッド:

```python
    def _execute_live_trigger(self, plan, pair, quote, decision_id, run_id) -> None:
        """本番発注の執行段 (spec §2, F-1/F-3)。single execution writer。

        claim (order_intent) → final gate → submit-marked → execute → 結果反映。
        例外は watch loop を止めない (recovery job が後で拾う)。
        """
        from datetime import timedelta

        from src.orchestrator.order_intent_lock import EXECUTION_LEASE_SECONDS  # later task が無ければ定数直書き
        from src.signals.signal_combiner import TradeSignal
        from src.trading.order_intent_status import (
            intent_status_for_outcome, is_alertable_outcome,
        )
        from src.utils.clock import db_now

        action = plan.action_json or {}
        try:
            # 1. claim (UNIQUE で二重発注防止)
            lease = db_now() + timedelta(seconds=120)
            claimed = self._orch.try_insert_order_intent(
                plan_id=plan.plan_id, pair=pair,
                intended_action=_side_of(plan.direction),
                owner_run_id=run_id, lease_until=lease, decision_id=decision_id,
            )
            if not claimed:
                logger.info(f"[ORCH] plan {plan.plan_id} already has order_intent — execute skipped")
                return

            # 1.5 material recheck (spec §2 step2、既定 OFF)。ON 時のみ ExecutionOpinionAgent
            #     を再点火する分岐をここに置く。F では flag 既定 False で常に skip
            #     (ON 時の実装は将来 task — 配線のプレースのみ)。
            if self._config.orchestrator.execution_opinion_recheck_enabled:
                logger.debug("[ORCH] material recheck flag on — (future) ExecutionOpinion 再点火")
                # 将来: material change なら ExecutionOpinionAgent 再点火 / timeout 超過なら保留

            # 2. final gate (RiskGate pre_check を hard gate として使う)
            draft = self._build_execution_draft(plan, pair, quote)  # 下で定義
            gate_ctx = self._build_gate_context(pair, quote)        # 下で定義
            gate = self._risk_gate.pre_check(draft, gate_ctx)
            if not gate.passed:
                self._orch.record_order_result(plan_id=plan.plan_id, status="rejected")
                self._orch.record_decision(
                    run_id=run_id, snapshot_id=0, pair=pair,
                    decision_type="plan_trigger", plan_id=plan.plan_id,
                    reasoning_summary=f"live gate reject: {gate.reject_class}",
                    risk_gate_result=gate.to_dict(),
                )
                logger.warning(f"[ORCH] live gate reject plan {plan.plan_id}: {gate.issues}")
                return  # plan 遷移は Task 5 で詳細化

            # 3. submit マーキング (復旧分岐点)
            self._orch.mark_order_submitted(plan_id=plan.plan_id, submitted_at=db_now())

            # 4. 発注 (single writer)
            signal = TradeSignal(
                pair=pair, action=_side_of(plan.direction),
                confidence=float(action.get("confidence", 1.0)),
                entry_price=quote.mid,
                stop_loss=float(action["sl"]), take_profit=float(action["tp"]),
            )
            result = self._execution_broker.execute_signal(
                signal, self._execution_position_mgr,
            )

            # 5. 結果反映 (outcome → status mapping)
            status = intent_status_for_outcome(result.outcome)
            order_id = result.order.order_id if result.is_executed else None
            self._orch.record_order_result(
                plan_id=plan.plan_id, status=status, order_id=order_id,
                broker_result_json={"outcome": result.outcome, "reason": result.reason},
            )
            self._orch.record_decision(
                run_id=run_id, snapshot_id=0, pair=pair,
                decision_type="plan_trigger", plan_id=plan.plan_id,
                reasoning_summary=f"live execute: {result.outcome}",
                order_id=order_id,
            )
            if is_alertable_outcome(result.outcome):
                logger.warning(f"[ORCH] live execute {result.outcome} plan {plan.plan_id}: {result.reason}")
        except Exception:
            logger.exception(f"[ORCH] live execute failed for plan {plan.plan_id}")
```

helper 2 つを同クラスに追加 (`ExecutionPlanDraft` は `src.orchestrator.schemas`、既存 `_shadow_risk_precheck` の draft 構築 `runtime.py:641` に倣う):

```python
    def _build_execution_draft(self, plan, pair, quote):
        from src.orchestrator.schemas import ExecutionPlanDraft
        action = plan.action_json or {}
        return ExecutionPlanDraft(
            pair=pair, direction=plan.direction, action=action,
        )  # 実フィールドは schemas.py:237 を確認して合わせる

    def _build_gate_context(self, pair, quote) -> dict:
        # final gate 用 context。既存 _shadow_risk_precheck が組む context 形に倣う
        # (quote.mid / technical status / risk_state)。実装時に既存 precheck を流用。
        return self._shadow_gate_context(pair, quote)  # 既存 helper があれば再利用
```

> `_build_execution_draft` / `_build_gate_context` の正確な構築は既存 `_shadow_risk_precheck` (`runtime.py:629`〜) を読んで同じ draft/context を作る (DRY)。可能なら `_shadow_risk_precheck` 内の draft/context 構築を helper に抽出して live 側と共有する。`record_decision` の `snapshot_id=0` は要確認 — trigger 時の snapshot_id を `_record_shadow_trigger` から渡せるなら渡す (引数追加)。`EXECUTION_LEASE_SECONDS` は定数 120 を直書きでよい (別 module 不要なら import 削除)。

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_taskf_execute_live_trigger.py -v`
Expected: PASS (4 passed)。helper 構築で詰まったら既存 runtime テストの構築を精読して合わせる。

- [ ] **Step 6: Regression**

Run: `pytest tests/test_orchestrator_runtime.py tests/test_orchestrator_e2e.py -q`
Expected: PASS (mode=shadow 既定で live 分岐に入らない = 既存 watch flow 不変)。

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_taskf_execute_live_trigger.py
git commit -m "feat: _execute_live_trigger 執行段 (claim→gate→submit→execute→反映) (Task F F-1/F-3)"
```

---

## Task 5: reject/failed/halted 後の plan/intent 遷移 (codex #4)

**Files:**
- Modify: `src/orchestrator/runtime.py` (`_execute_live_trigger` の reject/非executed 分岐に plan 遷移を追加)
- Test: `tests/test_taskf_reject_transitions.py`

**Note (codex #4):** trigger 時に plan は active→triggered に claim される (`runtime.py:569`)。`get_active_plans` は active のみ再評価 (`orchestrator_store.py:514`)。order_intent は plan_id UNIQUE。何もしないと plan は triggered のまま再評価されず order_intent が UNIQUE を握り**永久ブロック**。spec §4 の遷移表を実装する:
- **恒久的 (structural reject)** → plan を `invalidated`、order_intent は `rejected` のまま。再 trigger しない。
- **一時的 (fixable reject / failed / halted)** → order_intent を `abandoned` (UNIQUE 解放)、plan を `invalidated` (replan 既定 — 同一 plan の再 trigger ではなく新しい plan で発注。spec §4 の「replan 既定」)。
- **executed/skipped** → plan は triggered のまま (発注完了 or 用済み)。

> spec §4 確定方針: 一時的失敗でも**同一 plan の再 trigger はしない (replan 既定)**。よって plan は両 reject とも `invalidated` に倒し、order_intent の status のみ恒久=rejected / 一時=abandoned で区別する。これにより「永久ブロック」も「同一 plan_id 再 insert と UNIQUE の衝突」も避ける。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_reject_transitions.py` を新規作成:

```python
from src.trading.broker_adapter import ExecutionResult
# 共通 fixture は test_taskf_execute_live_trigger.py の helper を再利用 (import or 複製)。
# ここでは plan 遷移と order_intent status の最終状態を検証する。


def test_structural_reject_invalidates_plan_and_keeps_rejected(tmp_path):
    """恒久 reject: plan=invalidated, intent=rejected。再 trigger されない。"""
    rt = _runtime_with_execution(tmp_path, _FakeBroker(None), _GateReject("structural"))
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "rejected"
    plan = rt._orch.get_plan(plan_id)         # 既存 getter (無ければ get_active/all で確認)
    assert plan.status == "invalidated"
    # 再度 watch しても active でないので再評価されない
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "rejected"  # 変化なし


def test_fixable_reject_abandons_intent_and_invalidates_plan(tmp_path):
    """一時 reject: intent=abandoned (UNIQUE 解放), plan=invalidated (replan 既定)。"""
    rt = _runtime_with_execution(tmp_path, _FakeBroker(None), _GateReject("fixable"))
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "abandoned"
    assert rt._orch.get_plan(plan_id).status == "invalidated"


def test_failed_execution_abandons_intent(tmp_path):
    """broker failed (技術失敗) → intent=failed→abandoned 化で UNIQUE 解放 + plan invalidated。"""
    rt = _runtime_with_execution(
        tmp_path, _FakeBroker(ExecutionResult.failed("bridge down")), _GatePass(),
    )
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    intent = rt._orch.get_order_intent(plan_id)
    # 一時失敗扱い: replan で再発注できるよう UNIQUE を解放
    assert intent.status in ("failed", "abandoned")
    assert rt._orch.get_plan(plan_id).status == "invalidated"
```

> `_GateReject`/`_GatePass`/`_FakeBroker`/`_runtime_with_execution`/`_seed_active_plan_ready_to_trigger` は Task 4 のテスト helper を共有 (共通 conftest か module 複製 — 実装時に整理)。`get_plan(plan_id)` 相当の getter が無ければ既存 `get_active_plans` / 全件取得で plan status を確認する。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_reject_transitions.py -v`
Expected: FAIL — 現状 reject 後に plan を invalidated にせず intent も abandoned 化しない。

- [ ] **Step 3: Write minimal implementation**

`_execute_live_trigger` の gate reject 分岐と非 executed 分岐に plan/intent 遷移を追加。reject_class で恒久/一時を分岐:

```python
            if not gate.passed:
                permanent = gate.reject_class == "structural"
                if permanent:
                    self._orch.record_order_result(plan_id=plan.plan_id, status="rejected")
                else:
                    # 一時的: UNIQUE を解放して replan で再発注できるようにする
                    self._orch.record_order_result(plan_id=plan.plan_id, status="abandoned")
                self._orch.update_plan_status(plan.plan_id, "invalidated")  # replan 既定
                self._orch.record_decision(
                    run_id=run_id, snapshot_id=0, pair=pair,
                    decision_type="plan_invalidate", plan_id=plan.plan_id,
                    reasoning_summary=f"live gate reject ({gate.reject_class})",
                    risk_gate_result=gate.to_dict(),
                )
                return
```

実発注後の非 executed (skipped/halted/rejected/failed) 分岐:

```python
            status = intent_status_for_outcome(result.outcome)
            order_id = result.order.order_id if result.is_executed else None
            self._orch.record_order_result(
                plan_id=plan.plan_id, status=status, order_id=order_id,
                broker_result_json={"outcome": result.outcome, "reason": result.reason},
            )
            if not result.is_executed:
                # 発注に至らなかった: 一時失敗 (halted/failed) は replan 可能にするため
                # plan を invalidated にして UNIQUE は status で解放済 (abandoned/failed)。
                # skipped (想定内) も plan は用済み → invalidated。
                self._orch.update_plan_status(plan.plan_id, "invalidated")
```

> `decision_type="plan_invalidate"` が `DECISION_TYPES` (`orchestrator_store.py:137`) に含まれるか確認 (含まれる)。`failed→abandoned` への正規化が必要なら (UNIQUE 解放を確実にするため) record_order_result に `abandoned` を渡す方針も可 — テストは `("failed","abandoned")` を許容しているので、実装は「一時失敗は UNIQUE を解放する」意図が満たせればどちらでもよい。recovery job (Task 6) との整合: ここで invalidated + status 確定した plan は lease 超過前に解決済なので recovery 対象にならない。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_reject_transitions.py -v`
Expected: PASS (3 passed)。

- [ ] **Step 5: Regression**

Run: `pytest tests/test_taskf_execute_live_trigger.py tests/test_orchestrator_runtime.py -q`
Expected: PASS (Task 4 の正常系 + 既存が壊れていない)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_taskf_reject_transitions.py
git commit -m "feat: reject/failed 後の plan invalidate + intent UNIQUE 解放 (Task F, codex #4)"
```

---

## Task 6: 起動時 recovery job (F-2 の3分岐)

**Files:**
- Create: `src/orchestrator/order_recovery.py` (`recover_pending_intents`)
- Modify: `src/orchestrator/bootstrap.py` (起動時に recovery job を 1 回実行)
- Test: `tests/test_taskf_recovery_job.py`

**Note:** spec §3 の3分岐を Task 3 の `get_stale_or_unconfirmed_intents` の結果に対して実行する。`status=pending` → `retryable`、`status=submitted & order_id null` → `needs_reconcile` (再 trigger 禁止 + alert)、`status=submitted & order_id あり` → `filled` 補正。recovery job は純関数的に store API を叩くだけ (broker には触れない = 自動照合は F 外)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_recovery_job.py` を新規作成:

```python
from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.order_recovery import recover_pending_intents
from src.utils.clock import db_now


def _stale_intent(orch, *, plan_id):
    orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=db_now() - timedelta(seconds=60),
    )


def test_pending_becomes_retryable(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    _stale_intent(orch, plan_id=1)
    summary = recover_pending_intents(orch, now=db_now())
    assert orch.get_order_intent(plan_id=1).recovery_status == "retryable"
    assert summary["retryable"] == 1


def test_submitted_without_order_id_becomes_needs_reconcile(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent(orch, plan_id=2)
    orch.mark_order_submitted(plan_id=2, submitted_at=now)  # order_id まだ null
    summary = recover_pending_intents(orch, now=now)
    intent = orch.get_order_intent(plan_id=2)
    assert intent.recovery_status == "needs_reconcile"
    assert summary["needs_reconcile"] == 1


def test_submitted_with_order_id_corrected_to_filled(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent(orch, plan_id=3)
    orch.mark_order_submitted(plan_id=3, submitted_at=now)
    # order_id を直接持たせる (約定済だが status 補正前を模擬)
    orch.record_order_result(plan_id=3, status="submitted", order_id="mt5:1")
    # ただし lease 超過のまま (status=submitted, order_id あり) → 正常補正対象
    summary = recover_pending_intents(orch, now=now + timedelta(seconds=120))
    assert orch.get_order_intent(plan_id=3).status == "filled"
```

> 3 番目のテストは `get_stale_or_unconfirmed_intents` が `order_id あり submitted` を**拾わない** (Task 3) ため、別途 lease 超過 submitted+order_id を補正する経路が必要。実装時、recovery job は「status=submitted & order_id あり & lease 超過」も含めて `filled` 補正するなら、Task 3 の query にこのケースも足すか recovery job 内で別 query する。**簡潔さ優先:** 3 番目のケース (order_id あり) は「既に約定確定なので害は無い (UNIQUE は握るが正常)」とし、F の recovery job は **pending→retryable / submitted+null→needs_reconcile の 2 分岐のみ**に絞ってもよい (spec の3番目「正常 status 補正」は nice-to-have)。その場合 3 番目のテストは削除し、2 分岐で plan を作る。**実装者判断: まず 2 分岐 (retryable / needs_reconcile) を確実に。order_id あり補正は余裕があれば。**

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_recovery_job.py -v`
Expected: FAIL — `src.orchestrator.order_recovery` が無い。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/order_recovery.py` を新規作成:

```python
"""起動時 order_intent recovery job (spec §3, F-2)。

クラッシュで lease 超過した未完了 order_intent を 3 分岐で処理する:
- status=pending (未送信) → retryable (reconciliation 後に replan 可)
- status=submitted & order_id null (送信直後クラッシュ・建玉不明) → needs_reconcile
  (再 trigger 禁止 + alert。broker 自動照合は F 外 = 手動 / 既存 reconciliation)

broker には触れない (自動照合しない = スコープ境界)。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def recover_pending_intents(orch, *, now: datetime) -> dict[str, int]:
    """recovery 候補を列挙し recovery_status を確定する。集計 dict を返す。"""
    summary = {"retryable": 0, "needs_reconcile": 0}
    for intent in orch.get_stale_or_unconfirmed_intents(now=now):
        if intent.status == "pending":
            orch.set_recovery_status(plan_id=intent.plan_id, recovery_status="retryable")
            summary["retryable"] += 1
        elif intent.status == "submitted" and intent.order_id is None:
            orch.set_recovery_status(
                plan_id=intent.plan_id, recovery_status="needs_reconcile",
            )
            summary["needs_reconcile"] += 1
            logger.warning(
                "[ORCH-RECOVERY] needs_reconcile: plan %s submitted but order_id "
                "unknown — 再 trigger 禁止・要手動確認", intent.plan_id,
            )
    if summary["retryable"] or summary["needs_reconcile"]:
        logger.info("[ORCH-RECOVERY] %s", summary)
    return summary
```

> 上記は 2 分岐版 (実装者推奨)。order_id あり submitted の `filled` 補正を含めるなら、Task 3 の query を別途呼ぶか query を拡張し、3 番目の elif を足す。その場合 3 番目のテストを残す。2 分岐で進めるなら 3 番目のテストは削除。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_recovery_job.py -v`
Expected: PASS (2 分岐版なら 2 passed、3 分岐版なら 3 passed)。

- [ ] **Step 5: bootstrap で起動時に 1 回実行**

`src/orchestrator/bootstrap.py` の `build_orchestrator_runtime` で runtime 構築後 (return 直前) に recovery を実行:

```python
    # Task F: 起動時に前回クラッシュの未完了 order_intent を recovery 分類する (spec §3)。
    from src.orchestrator.order_recovery import recover_pending_intents
    from src.utils.clock import db_now
    recover_pending_intents(orch_store, now=db_now())
```

> mode=shadow でも実行して害は無い (order_intents が空なら no-op)。live のみに絞りたければ `if orch_cfg.mode == "live":` で囲む — shadow で order_intents が存在しないなら無条件でよい。

- [ ] **Step 6: Run bootstrap regression**

Run: `pytest tests/test_orchestrator_bootstrap.py -q`
Expected: PASS (recovery 呼び出しで既存 bootstrap が壊れていない)。

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/order_recovery.py src/orchestrator/bootstrap.py tests/test_taskf_recovery_job.py
git commit -m "feat: 起動時 order_intent recovery job (retryable/needs_reconcile) (Task F F-2)"
```

---

## Task 7: single execution writer — 旧 trading cycle entry 統一ガード (codex #3)

**Files:**
- Modify: `src/cycles/trading.py` (entry phase 直前に orchestrator.mode=live ガード)
- Test: `tests/test_taskf_single_writer_guard.py`

**Note (codex #3):** 旧 `run_trading_cycle` は main/API/CLI/TUI の 4 entry point から起動でき (`main.py:414` / `api/routes/trading.py:70` / `cli.py:369` / `tui.py:322`)、内部で `create_broker` (`trading.py:1026`) + `execute_signal` (`trading.py:754`) を呼ぶ。main.py の登録停止だけでは API/CLI/TUI 経由の発注が残る。`run_trading_cycle` 内部の entry phase を `OrchestratorConfig.mode=="live"` でガードすれば全 entry point を一括カバーする。**exit/close/reconciliation は触らない** (既存系の再利用は維持)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_single_writer_guard.py` を新規作成:

```python
from unittest.mock import patch


def test_trading_cycle_skips_entry_when_orchestrator_live(monkeypatch, tmp_path):
    """orchestrator.mode=live のとき run_trading_cycle が新規 entry (execute_signal) を呼ばない。"""
    from src.cycles import trading

    executed = []
    # execute_signal を呼ぶ箇所を監視 (実関数名は trading.py の entry 実行点に合わせる)。
    # 実装時: entry phase で broker.execute_signal を呼ぶ関数を patch し、
    # orchestrator.mode=live なら呼ばれない / shadow なら呼ばれることを確認する。
    ...


def test_trading_cycle_executes_entry_when_orchestrator_shadow(monkeypatch, tmp_path):
    """orchestrator.mode=shadow (既定) では従来通り entry を実行する (回帰)。"""
    ...
```

> 実装方針: `run_trading_cycle` の entry phase に「`config.orchestrator.mode == "live"` なら entry を skip」のガードを 1 箇所追加し、テストは config の orchestrator.mode を live/shadow に振って entry 実行点 (broker.execute_signal もしくはその呼出ラッパ) が呼ばれるかを mock で検証する。`run_trading_cycle` の構築は重いので、entry phase を担う内部関数があればそれを直接テストする (実装時に `trading.py:754` 周辺の関数構造を読んで最小テスト点を選ぶ)。本番発注は mock で起こさない。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_single_writer_guard.py -v`
Expected: FAIL — orchestrator.mode=live でも entry が実行される (ガード未実装)。

- [ ] **Step 3: Write minimal implementation**

`src/cycles/trading.py` の entry phase (新規 entry 発注 = `execute_signal` 呼出に進む直前、`trading.py:754` 周辺) に統一ガードを 1 箇所追加:

```python
    # Task F (codex #3): orchestrator が本番発注を担う (mode=live) なら、旧 cycle の
    # 新規 entry は停止する (single execution writer)。exit/close/reconciliation は継続。
    # main/API/CLI/TUI どの entry point から来てもここで一括ガードされる。
    if getattr(config.orchestrator, "mode", "shadow") == "live":
        logger.info("[CYCLE] orchestrator.mode=live — 新規 entry を skip (single writer)")
        # entry phase を抜けて exit/管理フェーズへ (return ではなく entry だけ skip)
    else:
        ... 既存の entry 発注処理 ...
```

> 実際の制御フローは `trading.py` の entry phase の構造に合わせる (entry が関数分離されていればその呼出を条件化、インラインなら entry ブロックを `if not live:` で囲む)。**exit/close/reconciliation を skip しない**ことに注意 (entry のみ)。`config.orchestrator.mode` への参照経路を確認 (config 引数の型)。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_single_writer_guard.py -v`
Expected: PASS。

- [ ] **Step 5: Regression (重要 — 旧 cycle の既存挙動)**

Run: `pytest tests/ -k "trading_cycle or trading" -q`
Expected: PASS (orchestrator.mode=shadow 既定で旧 cycle が従来通り entry 実行 = 全既存テスト不変)。

- [ ] **Step 6: Commit**

```bash
git add src/cycles/trading.py tests/test_taskf_single_writer_guard.py
git commit -m "feat: orchestrator.mode=live で旧 cycle entry を統一ガード (Task F single writer, codex #3)"
```

---

## Task 8: bootstrap で live 時に execution broker/position_mgr 注入 (F-4)

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (mode=live で execution broker + position_mgr 構築 + runtime 注入)
- Test: `tests/test_taskf_bootstrap_wiring.py`

**Note:** `build_orchestrator_runtime` は既に `orch_cfg = config.orchestrator` を持つ。Task 4 で runtime は `mode`/`execution_broker`/`execution_position_mgr` を受ける。live のとき発注用 broker を `create_broker(mode=config.mode, live_broker=..., ...)` で構築 (`src/cycles/trading.py:1026` の呼出に倣う)。execution 用 `PositionManager` は protect worker と同様 `PositionManager(StateStore(config.state_dir), context="OrchestratorExecution")` で構築。shadow では None (回帰)。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_bootstrap_wiring.py` を新規作成:

```python
def test_shadow_mode_no_execution_broker(tmp_path, monkeypatch):
    """mode=shadow (既定): runtime に execution_broker が注入されない (回帰)。"""
    # 実装時: 既存 test_orchestrator_bootstrap.py の build_orchestrator_runtime テストを流用。
    # orchestrator.mode=shadow で build し、rt._execution_broker is None を確認。
    ...


def test_live_mode_injects_execution_broker(tmp_path, monkeypatch):
    """mode=live: runtime に execution_broker + execution_position_mgr が注入される。"""
    # orchestrator.mode=live + AppConfig.mode=live + live_broker 設定で build し、
    # rt._execution_broker is not None / rt._mode == "live" を確認。
    # create_broker / PositionManager は monkeypatch で軽量 stub に差し替え (本番接続しない)。
    ...
```

> 実装時に既存 `tests/test_orchestrator_bootstrap.py` の構築パターン (Phase 2/D の `test_producer_covers_all_tradeable...` 等) を流用。`create_broker` と `PositionManager`/`StateStore` を monkeypatch で stub 化し、本番 broker 接続を起こさない。cross-field validation (Task 2) があるので live テストは AppConfig.mode=live + live_broker を満たす config を作る。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_bootstrap_wiring.py -v`
Expected: FAIL — bootstrap が execution broker を構築・注入しない。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/bootstrap.py` の `build_orchestrator_runtime` で、protect worker 構築ブロックの近く (runtime 構築前) に execution 配線を追加:

```python
    # Task F: orchestrator.mode=live なら発注用 execution broker + position_mgr を構築し
    # 執行段に注入する (single execution writer)。shadow では None で live 分岐に入らない。
    execution_broker = None
    execution_position_mgr = None
    if orch_cfg.mode == "live":
        from src.persistence.state_store import StateStore
        from src.trading.live_broker import create_broker
        from src.trading.position_manager import PositionManager

        execution_position_mgr = PositionManager(
            StateStore(config.state_dir), context="OrchestratorExecution"
        )
        # create_broker の引数は trading cycle (src/cycles/trading.py:1026) の呼出に合わせる。
        execution_broker = create_broker(
            mode=config.mode,
            live_broker=config.live_broker,  # 実フィールド経路は実装時に確認
            state_dir=config.state_dir,
            # mt5_* / lot / scale-in 等の引数は trading cycle 呼出と同じものを渡す
        )
```

`OrchestratorRuntime(...)` 呼び出しに引数追加:

```python
        mode=orch_cfg.mode,
        execution_broker=execution_broker,
        execution_position_mgr=execution_position_mgr,
```

> `create_broker` の全必須引数 (mt5_bridge_url / lot / magic / scale-in 等) は `src/cycles/trading.py:1026` の実呼出をコピーして同じ値を渡す (DRY — 可能なら trading cycle の broker 構築を helper 化して共有)。`config.live_broker` の正確な参照経路は schema を確認。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_bootstrap_wiring.py -v`
Expected: PASS。

- [ ] **Step 5: Regression**

Run: `pytest tests/test_orchestrator_bootstrap.py tests/test_orchestrator_e2e.py -q`
Expected: PASS (shadow 既定で execution 未注入 = 既存不変)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/bootstrap.py tests/test_taskf_bootstrap_wiring.py
git commit -m "feat: live 時に execution broker/position_mgr を執行段に注入 (Task F F-4)"
```

---

## Task 9: settings.yaml.example + 全体回帰 + Review Checklist

**Files:**
- Modify: `config/settings.yaml.example` (orchestrator.mode / execution_opinion_recheck_enabled のコメント)

- [ ] **Step 1: settings.yaml.example に記載**

`config/settings.yaml.example` の orchestrator セクション (`mode:` の近く) にコメント追記:

```yaml
  # Task F: shadow→本番発注 昇格。live は orchestrator 執行段が単一発注主体になる。
  # ※ live は top-level mode=live + live_broker 必須 (cross-field validation)。
  mode: "shadow"  # shadow | live
  # 発注直前の ExecutionOpinion 再点火 (material change 時)。既定 OFF (決定的・高速執行)。
  execution_opinion_recheck_enabled: false
```

- [ ] **Step 2: 全 F テスト**

Run: `pytest tests/test_taskf_order_intent_status_map.py tests/test_taskf_config_validation.py tests/test_taskf_recovery_query.py tests/test_taskf_execute_live_trigger.py tests/test_taskf_reject_transitions.py tests/test_taskf_recovery_job.py tests/test_taskf_single_writer_guard.py tests/test_taskf_bootstrap_wiring.py -v`
Expected: PASS (全て)。

- [ ] **Step 3: フルスイート回帰**

Run: `pytest -q`
Expected: 既存全 pass + 新規分。失敗ゼロ。**特に `OrchestratorConfig.mode=shadow` 既定で全既存挙動が無改変であること** (shadow 境界・旧 trading cycle entry 不変)。

- [ ] **Step 4: spec Review Checklist 確認**

spec §7 の各項目を対応テストで満たしているか確認:
- F-1 final gate / single writer / material OFF / outcome→status mapping → Task 1, 4
- F-2 UNIQUE 二重発注防止 / recovery query 送信直後クラッシュ / needs_reconcile 隔離 → Task 3, 6
- F-3 broker reject decision 反映 / reject 後遷移で永久ブロック回避 → Task 4, 5
- F-4 shadow 回帰 / cross-field validation / 旧 cycle entry 全 entry point 停止 → Task 2, 7, 8

- [ ] **Step 5: Commit**

```bash
git add config/settings.yaml.example
git commit -m "docs: settings.yaml.example に orchestrator.mode=live (Task F) 追記"
```

---

## 完了条件

- `OrchestratorConfig.mode=shadow` (既定) で全既存挙動が無改変 (回帰グリーン)。
- `mode=live` (+ AppConfig.mode=live + live_broker) で orchestrator 執行段が trigger→final gate→execute を single writer 実行。
- order_intents UNIQUE で二重発注を防ぎ、起動時 recovery が pending→retryable / submitted+null→needs_reconcile を分類 (送信直後クラッシュを取りこぼさない)。
- gate reject / 非 executed 後に plan が invalidated + intent UNIQUE 解放で永久ブロックしない。
- 旧 trading cycle の新規 entry が全 entry point (main/API/CLI/TUI) で停止 (single writer)。
- 旧経路コード削除 (omit) / needs_reconcile 自動照合 / material recheck ON 化は本 plan のスコープ外 (将来課題)。
