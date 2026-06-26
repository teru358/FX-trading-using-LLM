# Orchestrator Task F — shadow→本番発注 昇格 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** orchestrator の watch trigger を本番発注に結線し、`OrchestratorConfig.mode=shadow→live` で shadow→本番発注を昇格できるようにする (broker 結線 + durable order lock + クラッシュ復旧 + single execution writer)。

**Architecture:** live mode のとき `_record_shadow_trigger` 成功直後に同一 watch スレッド内で `_execute_live_trigger` を同期実行 (案A: claim-first → final gate → submit-marked → execute → 結果反映)。order_intents (plan_id UNIQUE) で二重発注を durable に防ぎ、起動時 recovery job がクラッシュ pending を 3 分岐で処理。旧 trading cycle の entry phase は `OrchestratorConfig.mode=live` 時に全 entry point で停止 (single execution writer)。

**Tech Stack:** Python 3, SQLAlchemy ORM (orchestrator_store), 既存 `BrokerAdapter`/`RiskGateWorker`/`TradeSignal`/`ExecutionPlanDraft`, pytest。

**Spec:** `docs/superpowers/specs/2026-06-23-orchestrator-taskF-shadow-to-live-execution-design.md`

---

## File Structure

**変更:**
- `src/config/schema.py` — `OrchestratorConfig.mode` validation (shadow/live)、`execution_opinion_recheck_enabled`、`AppConfig` の cross-field validation (2026-06-25 改訂: orchestrator.mode=live のとき AppConfig.mode=paper/live_test は許可〔段階検証〕、AppConfig.mode=live のときだけ live_broker 必須。Task 2 参照)。
- `src/data/orchestrator_store.py` — recovery query 拡張 (`get_stale_or_unconfirmed_intents`)、`set_recovery_status`。
- `src/orchestrator/runtime.py` — `_execute_live_trigger` (執行段)、`_record_shadow_trigger` の live 分岐、`__init__` に `execution_broker`/`execution_position_mgr`/`mode` 受け、reject 後の plan/intent 遷移、起動時 recovery job、F-5 観測性ログ (`_notify_planning_result` に 📋 plan created、執行段に ✅ live execute)。
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
- `tests/test_taskf_observability_logs.py`

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

**Note:** `OrchestratorConfig.mode: str = "shadow"` は既存 (`schema.py:720`)。トップレベル `AppConfig.mode: str = "paper"` (`schema.py:769`) と別物 = 発注先 broker を選ぶ軸 (paper/live/live_test)。発注 broker は `create_broker(mode=AppConfig.mode, live_broker=AppConfig.live_broker, ...)` で選ばれる (`live_broker.py:140`)。`OrchestratorConfig` 単体では AppConfig を参照できないので cross-field 検証は `AppConfig.__post_init__` (現状 **未定義 — 新設する**) に置く。`AppConfig.live_broker` は top-level フィールド (`schema.py:771` `live_broker: str | None = None`)。

**cross-field validation の方針 (2026-06-25 改訂、spec §5):** `orchestrator.mode=live` のとき、`AppConfig.mode` 別に:
- `paper` → **許可** (paper_broker は完全仮想で MT5 に一切到達しない。最も安全な段階検証。spec §0 確定判断7)。
- `live_test` → **許可するが実発注ガードが別途必要 (codex Critical、下記 §live_test 注意)**。
- `live` → `live_broker` 必須 (未設定なら実発注の取り違え事故として ValueError)。

旧版の「orchestrator.mode=live なら AppConfig.mode も必ず live」要求は paper 検証を弾くため撤回。`live_test` の `live_broker=mt5` 必須は `create_broker` 側が課すので AppConfig では二重に課さない。

> **§live_test 注意 — 実発注ガード (codex Critical):** `live_test` は paper primary + MT5 observer の `ShadowBrokerAdapter` を使うが、observer (`MT5BridgeBroker`) の `execute_signal` は **実際に bridge `/order` へ POST し、bridge が `DRY_RUN=false` なら MT5 `order_send` まで到達する** (`shadow_broker.py:69`, `mt5_bridge_broker.py:148`, `mt5_bridge/server.py:312`)。つまり `live_test` は「実発注しない」とは**コード上保証されていない** — bridge runtime の dry_run 状態に依存する。**config validation だけでは防げない (bridge 状態は config でなく runtime)。** よって bootstrap で execution broker を組む時 (Task 8) に **bridge `/health` の `dry_run==true` を確認し、false / 取得失敗なら起動を `ValueError` で弾く gate を必須とする** (`HealthResponse.dry_run`, `server.py:217-224`)。これにより `live_test` の価値 (MT5 observer でロット/margin 計算を検証) を残しつつ実発注事故を防ぐ。**最も安全な検証は `AppConfig.mode=paper`** (MT5 に一切到達しない) なので、まず paper を推奨し、live_test は MT5 経路の検証が要るときだけ dry_run gate 前提で使う。

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
        # Task F (spec §5, 2026-06-25 改訂): orchestrator が発注主体 (mode=live) のとき、
        # 発注先 broker を選ぶ top-level mode との整合を検証する。
        #   - AppConfig.mode=paper / live_test → 許可 (paper_broker で動作確認。本番資金を
        #     動かさない段階検証。spec §0 確定判断7)。
        #   - AppConfig.mode=live → live_broker 必須 (未設定なら「発注すると言いながら
        #     broker 未設定」の取り違え事故になるため弾く)。
        # 旧版の「orchestrator.mode=live なら AppConfig.mode も必ず live」要求は、
        # paper での段階検証を弾くため撤回 (spec §5 改訂)。
        if getattr(self.orchestrator, "mode", "shadow") == "live":
            if self.mode == "live" and self.live_broker is None:
                raise ValueError(
                    "orchestrator.mode=live with AppConfig.mode=live requires a "
                    "configured live_broker (mt5/oanda); got live_broker=None. "
                    "For dry-run validation use AppConfig.mode=paper or live_test."
                )
```

`AppConfig.live_broker` は schema の top-level フィールド (`schema.py:771` `live_broker: str | None = None`)。`create_broker(mode=..., live_broker=...)` (`src/cycles/trading.py:1026` の呼出) と同じ参照。`paper` / `live_test` は実発注しない (live_test は paper + observer、`live_broker.py:20`) ので broker 必須を課さない。`live_test` の `live_broker=mt5` 必須は `create_broker` 側 (`live_broker.py:213-217`) が課すため AppConfig では二重に課さない。

- [ ] **Step 6: Write the cross-field test + run**

`tests/test_taskf_config_validation.py` に追記 (AppConfig 構築は最小 fixture / loader 経由。実装時に既存 AppConfig テストの構築方法に倣う):

```python
def test_appconfig_orchestrator_live_paper_is_allowed():
    """orchestrator.mode=live + AppConfig.mode=paper は許可 (段階的 paper 検証、2026-06-25 改訂)。

    本番資金を動かさず paper_broker で F 経路を動作確認する正当な構成。ValueError にしない。
    """
    # 実装時: 既存 AppConfig 構築 helper / loader で最小 config を作り、
    # orchestrator.mode="live", top-level mode="paper" で例外が出ないことを確認。
    ...


def test_appconfig_orchestrator_live_live_test_is_allowed():
    """orchestrator.mode=live + AppConfig.mode=live_test も許可 (paper + observer 検証)。"""
    # live_test は実発注しない (paper + MT5 observer)。例外を出さない。
    # (live_broker=mt5 必須は create_broker 側が課すので AppConfig では検証しない)
    ...


def test_appconfig_orchestrator_live_with_top_level_live_requires_broker():
    """orchestrator.mode=live + AppConfig.mode=live + live_broker=None は ValueError。

    実発注モードなのに broker 未設定 = 取り違え事故を弾く (validation の本来の目的)。
    """
    # orchestrator.mode="live", top-level mode="live", live_broker=None で pytest.raises(ValueError)。
    ...


def test_appconfig_orchestrator_live_with_top_level_live_and_broker_ok():
    """orchestrator.mode=live + AppConfig.mode=live + live_broker=mt5 は成功 (本番発注構成)。"""
    # orchestrator.mode="live", top-level mode="live", live_broker="mt5" で例外なし。
    ...
```

> AppConfig の最小構築が必要。既存テストの構築パターン (`tests/test_config_loader.py`) を流用。検証する 4 ケース: (1) orchestrator.mode=live + mode=paper → 例外なし、(2) + mode=live_test → 例外なし、(3) + mode=live + live_broker=None → ValueError、(4) + mode=live + live_broker=mt5 → 例外なし。AppConfig 構築が重く mock が必要なら、最小限の loader 入力 dict で代替してよい。`live_broker` は top-level フィールド (`schema.py:771`)。

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

**Note (codex #1 + Medium 3分岐):** `mark_order_submitted` は `submitted_at` を埋めると同時に `status="submitted"` にする (`orchestrator_store.py:613-614`)。だが既存 `get_stale_pending_intents` は `status=="pending"` のみ拾う (`:597`)。よって「送信直後クラッシュ」(`status=submitted`) が拾われない。新メソッドで lease 超過の `status=="pending"` OR `status=="submitted"` を拾う (= recovery 3 分岐すべて: pending / submitted+order_id無 / submitted+order_id有 を Task 6 の job が order_id 有無で分岐)。terminal (filled/rejected/failed/abandoned) は対象外。既存 `get_stale_pending_intents` は他参照があり得るので**変更せず新メソッドを追加** (実装時に呼出元 grep)。

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


def test_picks_submitted_with_order_id_for_correction(tmp_path: Path):
    """order_id 付き submitted (status 補正前) も recovery 対象 (job が filled 補正、3 分岐目)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=3, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=3, submitted_at=now)
    orch.record_order_result(plan_id=3, status="submitted", order_id="mt5:111")
    rows = orch.get_stale_or_unconfirmed_intents(now=now)
    assert [r.plan_id for r in rows] == [3]


def test_skips_terminal_filled(tmp_path: Path):
    """既に filled (terminal) は recovery 対象外。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _insert(orch, plan_id=6, lease_offset_sec=-60)
    orch.mark_order_submitted(plan_id=6, submitted_at=now)
    orch.record_order_result(plan_id=6, status="filled", order_id="mt5:222")
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
        """recovery 候補を返す (Task F, codex #1)。lease 超過のうち未完了 = recovery 3 分岐:
        status=="pending" (未送信) OR status=="submitted" (送信後)。後者は order_id 有無で
        recovery job が needs_reconcile / filled 補正に分岐する。terminal (filled/rejected/
        failed/abandoned) は対象外。'pending only' の既存 get_stale_pending_intents だと
        submitted (送信直後クラッシュ) を取りこぼすため別メソッドにする。
        """
        from sqlalchemy import or_

        with Session(self._engine) as session:
            stmt = (
                select(_OrderIntent)
                .where(_OrderIntent.lease_until < now)
                .where(
                    or_(
                        _OrderIntent.status == "pending",
                        _OrderIntent.status == "submitted",
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
- Modify: `src/orchestrator/runtime.py` (`__init__` に `mode`/`execution_broker`/`execution_position_mgr`/`app_config` 追加、`_execute_live_trigger` 新規、`_record_shadow_trigger` の live 分岐、reject 遷移 `_apply_reject_transition`)
- Test: `tests/test_taskf_execute_live_trigger.py`, `tests/test_taskf_reject_transitions.py`

**Note:** 既存 `OrchestratorRuntime.__init__` は `risk_gate` を持つ (`runtime.py:71` `self._risk_gate`)。**live 分岐の挿入位置は通知直前ではなく `try` 内・shadow_trigger 記録成功直後 (run が open のうち) — codex High、上記「挿入位置」参照。** `RiskGateWorker.pre_check(draft, context) -> RiskGateResult(passed, reject_class, issues)` (`risk_gate.py:83`、reject_class は "structural"=恒久 / "fixable"=一時)。`try_insert_order_intent(*, plan_id, pair, intended_action, owner_run_id, lease_until, decision_id=None, trigger_id=None) -> bool` (`orchestrator_store.py:527`)。`broker.execute_signal(signal, position_mgr, macro_context="") -> ExecutionResult` (`broker_adapter.py:66`)。`TradeSignal(pair, action, confidence, entry_price, stop_loss, take_profit, ...)` (`signal_combiner.py:21`)。`record_order_result(*, plan_id, status, order_id=None, broker_result_json=None)`。`mark_order_submitted(*, plan_id, submitted_at)`。

**AppConfig 注入 (codex High — sizing 用):** position_size の risk 算出 (`config.trading.risk_per_trade` 等) は **AppConfig** にあり、`self._config` (= OrchestratorConfig、`bootstrap.py:270`) からは読めない。よって runtime に **AppConfig (または最小限 trading 設定 + instrument pip_value) を `app_config` として注入**する (bootstrap が `config` を渡す)。`_trade_signal_from_plan` にこれと execute 時点 balance を渡す。

**重要 (この task の範囲、codex Medium — Task 5 統合):** live 経路の不変条件として、**gate reject 後の plan/intent 遷移 (旧 Task 5) を本 task に統合する**。Task 4 だけ先にコミットすると、gate reject 後に plan が `triggered` のまま残り `get_active_plans` (`orchestrator_store.py:514`) で再評価されない**永久ブロックの中間状態**が生まれるため (codex Medium)。よって `_execute_live_trigger` は reject/failed/halted 時に `_apply_reject_transition` で plan を terminal 化 (一時的→abandoned+invalidated / 恒久的→invalidated、spec §4) し、intent も terminal status にする。旧 Task 5 のテスト (`test_taskf_reject_transitions.py`) も本 task で実装する (Step は下記に追加)。

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

**挿入位置 (codex High — 修正必須):** 当初案の「通知直前 (`if not ok: return False` の後)」は **不正**。`_record_shadow_trigger` の構造は `try: ... shadow_trigger 記録 ... ok=True / finally: finish_run(run_id) / (finally 後) 通知` (`runtime.py:615-633`)。通知直前は **`finally` の後＝`finish_run` 済み**。ここで live execution を呼ぶと、`_execute_live_trigger` 内の `record_decision(run_id, ...)` が**終了済み run に後付け**され、execute 失敗しても run は `ok` のまま残り、`snapshot_id=0` も汚染する。

正しくは **`try` ブロック内・shadow_trigger 記録成功直後 (ok=True の直後、まだ run が open のうち)** に挿入し、`snapshot_id` / `shadow_trigger_id` / `trigger_ctx` を渡す。execute 失敗時は同一 try が `ok=False` に倒し、`finally` が `finish_run(status="failed")` を記録する。これにより run lifecycle と execution の整合が保たれる。

```python
        # try 内、shadow_trigger 記録成功・ok=True の直後 (run が open のうち):
        # Task F: live mode かつ execution broker 注入時のみ本番発注へ進む (single writer)。
        if self._mode == "live" and self._execution_broker is not None:
            # snapshot_id / shadow_trigger_id を渡し、record_decision が open run に正しく紐づく。
            # 例外は _execute_live_trigger 内で握り、ok を倒さない or 倒すかは結果で決める
            # (executed/skipped=正常終了、reject/failed=run は ok のままで intent 側に記録、
            #  例外=recovery 対象として finally で failed)。詳細は下記実装。
            self._execute_live_trigger(
                plan, pair, quote, decision_id, shadow_trigger_id, snapshot_id, run_id,
            )
```

> 代替案: execution を別の execution run (`start_run(trigger_type="live_execute")`) で起こす設計も可。ただし shadow_trigger と同一 run に紐づける方が trace graph (§8.1) が単純。実装時にどちらか確定し、いずれにせよ **finish_run 後に record_decision しない**ことを不変条件とする。`_execute_live_trigger` の record_decision には `snapshot_id=0` でなく渡された `snapshot_id` を使う。

`_record_shadow_trigger` の下に新メソッド:

```python
    def _execute_live_trigger(
        self, plan, pair, quote, decision_id, shadow_trigger_id, snapshot_id, run_id,
    ) -> None:
        """本番発注の執行段 (spec §2, F-1/F-3)。single execution writer。

        claim (order_intent) → final gate → submit-marked → execute → 結果反映。
        例外は watch loop を止めない (recovery job が後で拾う)。
        run はまだ open (呼び出しは finish_run より前)。record_decision には渡された
        snapshot_id を使う (snapshot_id=0 を使わない、codex High)。
        """
        from datetime import timedelta

        from src.trading.order_intent_status import (
            intent_status_for_outcome, is_alertable_outcome,
        )
        from src.utils.clock import db_now
        # _trade_signal_from_plan は Task 4.5 で同 module (runtime.py) に定義する module-level helper。

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
            #     注意: self._config は OrchestratorConfig そのもの (bootstrap が config=orch_cfg
            #     で渡す, runtime.py:61/bootstrap.py:270)。.orchestrator は付けない (codex Medium)。
            if self._config.execution_opinion_recheck_enabled:
                logger.debug("[ORCH] material recheck flag on — (future) ExecutionOpinion 再点火")
                # 将来: material change なら ExecutionOpinionAgent 再点火 / timeout 超過なら保留

            # 2. final gate (RiskGate pre_check を hard gate として使う)
            draft = self._build_execution_draft(plan, pair, quote)  # 下で定義
            gate_ctx = self._build_gate_context(pair, quote)        # 下で定義
            gate = self._risk_gate.pre_check(draft, gate_ctx)
            if not gate.passed:
                self._orch.record_order_result(plan_id=plan.plan_id, status="rejected")
                self._orch.record_decision(
                    run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                    decision_type="plan_trigger", plan_id=plan.plan_id,
                    reasoning_summary=f"live gate reject: {gate.reject_class}",
                    risk_gate_result=gate.to_dict(),
                )
                logger.warning(f"[ORCH] live gate reject plan {plan.plan_id}: {gate.issues}")
                # reject 後の plan/intent 遷移はこの Task 内で実施 (codex Medium: Task 5 と統合)。
                self._apply_reject_transition(plan, gate.reject_class)  # 下記 (旧 Task 5)
                return

            # 3. submit マーキング (復旧分岐点)
            self._orch.mark_order_submitted(plan_id=plan.plan_id, submitted_at=db_now())

            # 4. 発注 (single writer)。TradeSignal は全必須フィールドを満たす helper で構築
            #    (codex High: 現行 dataclass は predicted_direction/combined_score/position_size/
            #     signal_reason/detail_reason/news/price/generated_at も必須)。
            #    position_size は risk ベース算出 (codex High): config + execute 時点 balance を渡す。
            balance = self._execution_position_mgr.get_balance()  # 実メソッド名は実装時に確認
            signal = _trade_signal_from_plan(plan, pair, quote, self._app_config, balance)
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

> 注意: この task の `_execute_live_trigger` は `_trade_signal_from_plan` を呼ぶが、その helper は **Task 4.5** で定義する。Task 4 のテストを通すには Task 4.5 の helper が先に必要なので、**Task 4.5 を Task 4 と同時に実装**してよい (テスト RED→GREEN の順は: Task 4.5 helper の単体テスト → Task 4 の execute テスト)。subagent-driven 実行時は Task 4 dispatch 内で helper も書く形でよい。

---

## Task 4.5: `_trade_signal_from_plan` — plan から完全な TradeSignal を構築 (codex High)

**Files:**
- Modify: `src/orchestrator/runtime.py` (module-level helper `_trade_signal_from_plan`)
- Test: `tests/test_taskf_trade_signal_from_plan.py`

**Note (codex High):** 現行 `TradeSignal` は 15 フィールド: `pair, action, predicted_direction, combined_score, confidence, entry_price, stop_loss, take_profit, position_size, signal_reason, detail_reason, news (NewsSentiment), price (PriceAnalysis), generated_at, tv_recommendation=""` (`signal_combiner.py:21`)。broker (`mt5_bridge_broker.execute_signal`) が読むのは `action / pair / position_size (→volume_lots) / stop_loss / take_profit / signal_reason (→comment) / confidence` (`mt5_bridge_broker.py:96-136`)。helper は plan の `action_json` (sl/tp/rr/confidence) と quote から broker 関連フィールドを埋め、provenance フィールド (news/price/predicted_direction 等) は orchestrator 由来と分かる最小有効値で埋める。`NewsSentiment(pair, sentiment_score, confidence, ...)` (`news_analyzer.py:45`) と `PriceAnalysis(pair, direction_bias, bias_score, confidence, entry_zone, reasoning_summary, analyzed_at, ...)` (`price_analyzer.py:36`) は必須フィールドのみ渡して最小構築する。

**position_size (codex High — 修正):** `ExecutionPlanDraft.action` には `position_size` が**無い** (`schemas.py:240` は `sl/tp/size_policy/rr/comment`)。よって固定 `1000.0` fallback は使わず、**既存 trading cycle と同一の `_calculate_position_size(balance, risk_pct, entry, stop_loss, pip_value, min_lot_size, lot_unit)` (`signal_combiner.py:251`、trading cycle は `trading.py:306` で使用) を再利用**する。helper は `config` (risk_per_trade/min_lot_size/lot_unit を `config.trading` から、pip_value を instrument 設定から) と `balance` (現在残高) を受け取る。balance / pip_value の取得経路は既存 trading cycle の発注前処理 (`trading.py:301-308` 付近) に倣う (実装時に確認)。これにより orchestrator 発注も既存と同じリスク管理 (口座リスク % ベース) になる。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_trade_signal_from_plan.py` を新規作成:

```python
from datetime import datetime
from types import SimpleNamespace

from src.orchestrator.runtime import _trade_signal_from_plan
from src.signals.signal_combiner import TradeSignal


def _plan():
    return SimpleNamespace(
        plan_id=1, pair="USDJPY=X", direction="long",
        action_json={"sl": 149.0, "tp": 152.0, "rr": 2.0, "confidence": 0.7},
    )


def _quote():
    return SimpleNamespace(mid=150.0, bid=150.0, ask=150.02, spread=0.02,
                           source="mt5", observed_at=datetime.now())


def _config():
    # 実装時: 既存 AppConfig 構築 helper / 最小 fixture。trading.risk_per_trade/
    # min_lot_size/lot_unit と instrument の pip_value を持つ最小 config。
    ...


def test_builds_valid_tradesignal_with_all_fields():
    sig = _trade_signal_from_plan(_plan(), "USDJPY=X", _quote(), _config(), balance=50000.0)
    assert isinstance(sig, TradeSignal)        # 全必須フィールドが揃い construct 成功
    assert sig.pair == "USDJPY=X"
    assert sig.action == "buy"                 # long → buy
    assert sig.stop_loss == 149.0
    assert sig.take_profit == 152.0
    assert sig.confidence == 0.7
    assert sig.entry_price == 150.0            # quote.mid
    # broker が comment に使う signal_reason が空でない
    assert sig.signal_reason
    # provenance フィールドも有効値 (news/price は dataclass インスタンス)
    assert sig.news is not None and sig.price is not None


def test_short_direction_maps_to_sell():
    plan = _plan()
    plan.direction = "short"
    sig = _trade_signal_from_plan(plan, "USDJPY=X", _quote(), _config(), balance=50000.0)
    assert sig.action == "sell"


def test_position_size_uses_risk_based_calc_not_fixed_1000():
    """codex High: position_size は固定 1000 でなく、_calculate_position_size による
    risk ベース算出 (balance × risk_per_trade / SL距離, min_lot/unit 丸め) になる。"""
    # balance / risk_per_trade / SL距離 を変えると position_size が変わることを確認
    # (固定 1000 なら不変になってしまう)。min_lot_size 下限・lot_unit 丸めも確認。
    sig = _trade_signal_from_plan(_plan(), "USDJPY=X", _quote(), _config(), balance=50000.0)
    assert sig.position_size and sig.position_size > 0
    # 既存 _calculate_position_size と同じ値になること (同一引数で直接呼んで突き合わせ)。
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_trade_signal_from_plan.py -v`
Expected: FAIL — `_trade_signal_from_plan` が無い (ImportError)。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/runtime.py` の module-level (クラス外、import 群の下) に追加:

```python
def _trade_signal_from_plan(plan, pair: str, quote, config, balance: float) -> "TradeSignal":
    """plan の action_json + quote から発注用 TradeSignal を構築する (Task F)。

    broker が読むフィールド (action/pair/position_size/stop_loss/take_profit/
    signal_reason/confidence) を plan から埋め、provenance フィールド (news/price/
    predicted_direction 等) は orchestrator 由来と分かる最小有効値で埋める。
    TradeSignal は 15 フィールド必須 (signal_combiner.py:21) なので全て渡す。

    position_size (codex High): ExecutionPlanDraft.action には position_size が無い
    (schemas.py:240 は sl/tp/size_policy/rr/comment のみ) ため、既存 trading cycle と
    同じ risk ベース算出を再利用する。固定 1000.0 fallback は risk_per_trade/
    min_lot_size/lot_unit を無視するため使わない。
    """
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from src.signals.signal_combiner import TradeSignal, _calculate_position_size
    from src.utils.clock import db_now

    action = plan.action_json or {}
    side = "buy" if plan.direction == "long" else "sell"
    bias = "bullish" if plan.direction == "long" else "bearish"
    pbias = "long" if plan.direction == "long" else "short"
    sl = float(action["sl"])
    tp = float(action["tp"])
    conf = float(action.get("confidence", 1.0))
    # 既存 trading cycle (trading.py:306) と同一の risk ベース算出を再利用する。
    # pip_value は instrument 設定から (既存 trading cycle の取得経路に倣う、実装時に確認)。
    pip_value = _resolve_pip_value(config, pair)  # 既存経路を流用 (trading cycle と同じ instrument 設定)
    size = _calculate_position_size(
        balance=balance,
        risk_pct=config.trading.risk_per_trade,
        entry=quote.mid,
        stop_loss=sl,
        pip_value=pip_value,
        min_lot_size=config.trading.min_lot_size,
        lot_unit=config.trading.lot_unit,
    )
    now = db_now()

    news = NewsSentiment(pair=pair, sentiment_score=0.0, confidence=conf)
    price = PriceAnalysis(
        pair=pair, direction_bias=pbias, bias_score=0.0, confidence=conf,
        entry_zone=(quote.mid, quote.mid), reasoning_summary="orchestrator plan",
        analyzed_at=now, stop_loss=sl, take_profit=tp,
        risk_reward_ratio=float(action.get("rr", 0.0)),
    )
    return TradeSignal(
        pair=pair, action=side, predicted_direction=bias,
        combined_score=0.0, confidence=conf, entry_price=quote.mid,
        stop_loss=sl, take_profit=tp, position_size=size,
        signal_reason=f"orchestrator plan {plan.plan_id}",
        detail_reason="orchestrator watch trigger (Task F live execute)",
        news=news, price=price, generated_at=now,
    )
```

> `NewsSentiment` / `PriceAnalysis` の必須フィールドは実装時に再確認 (`news_analyzer.py:45` / `price_analyzer.py:36`)。本番発注で broker が実際に使うのは sl/tp/size/comment/confidence なので、provenance は最小有効値でよい (発注の意思決定は既に plan で確定済)。
>
> **`_resolve_pip_value` / balance の取得 (codex High):** `pip_value` は既存 trading cycle が instrument 設定から得ている経路を流用する (実装時に `trading.py:301-308` 付近の `_calculate_position_size` 呼出を確認し、同じ pip_value source を使う。helper 化できるならそれを共有)。`balance` は execute 時点の口座残高で、`_execute_live_trigger` (Task 4) が `execution_position_mgr` / StateStore から取得して helper に渡す (既存 trading cycle と同じ残高ソース)。これにより plan→発注の間に残高が動いても execute 時点の最新 balance でロットが決まる (orchestrator の逐次原則と整合)。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_trade_signal_from_plan.py -v`
Expected: PASS (3 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_taskf_trade_signal_from_plan.py
git commit -m "feat: _trade_signal_from_plan で完全な TradeSignal を構築 (Task F, codex High)"
```

---

## Task 5: reject/failed/halted 後の plan/intent 遷移 (codex #4) — **Task 4 と同一コミット**

> **codex Medium:** 本 task (reject 後遷移) は Task 4 (`_execute_live_trigger`) と**同一コミットにまとめる**。Task 4 だけ先にコミットすると、gate reject 後に plan が `triggered` のまま残り再評価されない永久ブロックの中間状態が live 経路に入るため。実装手順としては Task 4 の `_execute_live_trigger` に `_apply_reject_transition` (本 task) を組み込んでから 1 コミットする。Task 4 の Step の中で本 task の test (`test_taskf_reject_transitions.py`) も green にする。下記はその遷移ロジックの詳細仕様。

**Files:**
- Modify: `src/orchestrator/runtime.py` (`_execute_live_trigger` の reject/非executed 分岐に plan 遷移 `_apply_reject_transition` を追加 — Task 4 と同一変更)
- Test: `tests/test_taskf_reject_transitions.py`

**Note (codex #4 + High#2):** trigger 時に plan は active→triggered に claim される (`runtime.py:569`)。`get_active_plans` は active のみ再評価 (`orchestrator_store.py:514`)。order_intent は plan_id UNIQUE で、**行が存在する限り UNIQUE を握り続ける (status を `abandoned` 等に変えても解放されない、codex High#2)**。何もしないと plan は triggered のまま再評価されず永久ブロック。

**確定モデル (spec §4 の replan、同一 plan を蘇生させない):** reject/非executed はすべて旧 plan を `invalidated` (terminal) にし、旧 intent も terminal status にする。**再発注は「同一 plan_id を再利用」ではなく、次の planning サイクルが新しい plan (= 新 plan_id → 新 order_intent) を作る**ことで行う (旧 intent 行は残るが新 plan_id なので UNIQUE 衝突しない)。`reject_class` で intent.status のみ区別する:
- **恒久的 (structural)** → intent `rejected` / plan `invalidated`。
- **一時的 (fixable)** → intent `abandoned` / plan `invalidated`。
- **failed (broker 技術失敗)** → intent `failed` / plan `invalidated`。
- **halted** → intent `rejected` / plan `invalidated`。
- **skipped** → intent `abandoned` / plan `invalidated`。
- **executed** → intent `filled` / plan `triggered` のまま。

> どの reject/非executed でも plan は `invalidated`。intent status は provenance のため区別するが、UNIQUE は行の存在で握り続ける (status では解放されない) — 再発注は新 plan_id で行うのでこれで問題ない。テストは「plan=invalidated かつ次 watch で再 trigger されない」を pin する (intent status は区別を確認する程度)。

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
    plan = rt._orch.get_trade_plan(plan_id)   # 実 API は get_trade_plan (orchestrator_store.py:464)
    assert plan.status == "invalidated"
    # 再度 watch しても active でないので再評価されない (同一 plan は蘇生しない)
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "rejected"  # 変化なし


def test_fixable_reject_abandons_intent_and_invalidates_plan(tmp_path):
    """一時 reject: intent=abandoned (provenance 区別), plan=invalidated。再発注は新 plan で。"""
    rt = _runtime_with_execution(tmp_path, _FakeBroker(None), _GateReject("fixable"))
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "abandoned"
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"


def test_failed_execution_invalidates_plan(tmp_path):
    """broker failed (技術失敗) → intent=failed, plan=invalidated。再発注は新 plan で。"""
    rt = _runtime_with_execution(
        tmp_path, _FakeBroker(ExecutionResult.failed("bridge down")), _GatePass(),
    )
    plan_id = rt._seed_active_plan_ready_to_trigger()
    rt.run_watch_cycle()
    assert rt._orch.get_order_intent(plan_id).status == "failed"
    assert rt._orch.get_trade_plan(plan_id).status == "invalidated"
```

> `_GateReject`/`_GatePass`/`_FakeBroker`/`_runtime_with_execution`/`_seed_active_plan_ready_to_trigger` は Task 4 のテスト helper を共有 (共通 conftest か module 複製 — 実装時に整理)。`get_trade_plan(plan_id)` が実 API (`orchestrator_store.py:464`)。**注意:** intent.status を `abandoned` 等にしても UNIQUE 行は残る (解放されない) — 再発注は新 plan_id で行う前提なので問題ない (codex High#2)。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_reject_transitions.py -v`
Expected: FAIL — 現状 reject 後に plan を invalidated にせず intent も abandoned 化しない。

- [ ] **Step 3: Write minimal implementation**

`_execute_live_trigger` の gate reject 分岐と非 executed 分岐に plan/intent 遷移を追加。reject_class で恒久/一時を分岐:

```python
            if not gate.passed:
                # 恒久 (structural)=rejected / 一時 (fixable)=abandoned で intent.status を区別。
                # どちらも plan は invalidated (terminal)。再発注は新 plan_id で行う (replan)
                # ため UNIQUE 行は残ってよい (codex High#2: status では UNIQUE は解放されない)。
                intent_status = "rejected" if gate.reject_class == "structural" else "abandoned"
                self._orch.record_order_result(plan_id=plan.plan_id, status=intent_status)
                self._orch.update_plan_status(plan.plan_id, "invalidated")  # terminal (replan で再発注)
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
                # 発注に至らなかった (skipped/halted/rejected/failed): plan を terminal 化。
                # 再発注は次 planning が新 plan を作る (replan)。UNIQUE 行は残るが新 plan_id
                # なので衝突しない (codex High#2: status 変更では UNIQUE 解放されない)。
                self._orch.update_plan_status(plan.plan_id, "invalidated")
```

> `decision_type="plan_invalidate"` が `DECISION_TYPES` (`orchestrator_store.py:137`) に含まれるか確認 (含まれる)。intent.status は `intent_status_for_outcome` の mapping (executed→filled / skipped→abandoned / rejected→rejected / halted→rejected / failed→failed) をそのまま使う — terminal status であればよく、UNIQUE は行存在で握り続けるが再発注は新 plan_id なので問題ない (codex High#2)。recovery job (Task 6) との整合: ここで invalidated + terminal status を確定した intent は lease 超過しても terminal なので recovery 対象 (`get_stale_or_unconfirmed_intents` は pending/submitted のみ) にならない。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_reject_transitions.py -v`
Expected: PASS (3 passed)。

- [ ] **Step 5: Regression**

Run: `pytest tests/test_taskf_execute_live_trigger.py tests/test_orchestrator_runtime.py -q`
Expected: PASS (Task 4 の正常系 + 既存が壊れていない)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_taskf_reject_transitions.py
git commit -m "feat: reject/failed 後の plan invalidate + intent terminal 化 (Task F, codex #4)"
```

---

## Task 6: 起動時 recovery job (F-2 の3分岐)

**Files:**
- Create: `src/orchestrator/order_recovery.py` (`recover_pending_intents`)
- Modify: `src/orchestrator/bootstrap.py` (起動時に recovery job を 1 回実行)
- Test: `tests/test_taskf_recovery_job.py`

**Note (spec §3、codex High + Medium):** spec §3 の **3 分岐** を Task 3 の `get_stale_or_unconfirmed_intents` の結果に対して実行する。recovery_status を更新するだけでは plan は再 trigger されない (plan は triggered claim 済、`get_active_plans` は active のみ — codex High)。よって §4 と同じ **replan モデル**: 旧 plan/intent を terminal 化し、再発注は次 planning が新 plan を作る。

| 状態 | recovery_status | intent.status | plan 状態 | 備考 |
|---|---|---|---|---|
| `status=pending` (未送信クラッシュ) | `retryable` | `abandoned` (terminal) | `invalidated` | 未発注。新 plan で再発注 |
| `status=submitted` & `order_id` null (送信直後クラッシュ・建玉不明) | `needs_reconcile` | `submitted` のまま (触らない) | `triggered` のまま (隔離) | **再 trigger 禁止 + alert**。建玉あるかもしれず terminal 化しない。手動/既存 reconciliation |
| `status=submitted` & `order_id` あり (約定確定・status 補正前) | (なし) | `filled` に補正 | `triggered` のまま | 正常約定 |

recovery job は store API を叩くだけ (broker には触れない = 自動照合は F 外)。`get_trade_plan` で plan を引き `update_plan_status` で遷移。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_recovery_job.py` を新規作成:

```python
from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.order_recovery import recover_pending_intents
from src.utils.clock import db_now


def _stale_intent_with_plan(orch, *, plan_id):
    """triggered 済 plan + lease 超過 intent を作る (クラッシュ状況を模擬)。

    実装時: 既存 plan 作成 helper で plan を作り update_plan_status(triggered) し、
    try_insert_order_intent で intent を入れる。plan 作成 API は orchestrator_store の
    既存テストに倣う (record_trade_plan 等)。
    """
    # ... 既存 plan 作成 + triggered 化 + intent insert (実装時に既存テストパターン流用)
    orch.try_insert_order_intent(
        plan_id=plan_id, pair="USDJPY=X", intended_action="buy",
        owner_run_id=1, lease_until=db_now() - timedelta(seconds=60),
    )


def test_pending_becomes_retryable_and_invalidates_plan(tmp_path: Path):
    """未送信クラッシュ: recovery_status=retryable, intent=abandoned, plan=invalidated。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    _stale_intent_with_plan(orch, plan_id=1)
    summary = recover_pending_intents(orch, now=db_now())
    intent = orch.get_order_intent(plan_id=1)
    assert intent.recovery_status == "retryable"
    assert intent.status == "abandoned"             # terminal 化 (新 plan で再発注)
    assert orch.get_trade_plan(1).status == "invalidated"
    assert summary["retryable"] == 1


def test_submitted_without_order_id_becomes_needs_reconcile(tmp_path: Path):
    """送信直後クラッシュ: needs_reconcile, plan は triggered のまま隔離 (terminal 化しない)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent_with_plan(orch, plan_id=2)
    orch.mark_order_submitted(plan_id=2, submitted_at=now)  # order_id まだ null
    summary = recover_pending_intents(orch, now=now)
    intent = orch.get_order_intent(plan_id=2)
    assert intent.recovery_status == "needs_reconcile"
    assert intent.status == "submitted"             # 触らない (建玉あるかもしれない)
    assert orch.get_trade_plan(2).status == "triggered"  # terminal 化しない (隔離)
    assert summary["needs_reconcile"] == 1


def test_submitted_with_order_id_corrected_to_filled(tmp_path: Path):
    """約定確定だが status 補正前: status を filled に補正 (3 分岐目、codex Medium)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    _stale_intent_with_plan(orch, plan_id=3)
    orch.mark_order_submitted(plan_id=3, submitted_at=now)
    orch.record_order_result(plan_id=3, status="submitted", order_id="mt5:1")  # 約定済模擬
    summary = recover_pending_intents(orch, now=now + timedelta(seconds=120))
    assert orch.get_order_intent(plan_id=3).status == "filled"
    assert summary["corrected_filled"] == 1
```

> Task 3 で `get_stale_or_unconfirmed_intents` は `status=submitted & order_id あり` も拾う形に拡張済 (3 分岐すべてを recovery job が見る)。`_stale_intent_with_plan` は plan を triggered で作る必要がある (plan 遷移を検証するため) — 既存 orchestrator_store テストの plan 作成 API を流用。`needs_reconcile` だけ plan を triggered のまま残す (隔離) ことに注意。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_recovery_job.py -v`
Expected: FAIL — `src.orchestrator.order_recovery` が無い。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/order_recovery.py` を新規作成:

```python
"""起動時 order_intent recovery job (spec §3, F-2)。

クラッシュで lease 超過した未完了 order_intent を 3 分岐で処理する (replan モデル:
旧 plan/intent を terminal 化し、再発注は次 planning が新 plan を作る)。
- status=pending (未送信) → retryable: intent=abandoned + plan=invalidated (新 plan で再発注)
- status=submitted & order_id null (送信直後クラッシュ・建玉不明) → needs_reconcile:
  intent/plan は触らず隔離 + alert (建玉あるかもしれない。再 trigger 禁止、自動照合は F 外)
- status=submitted & order_id あり (約定確定・status 補正前) → intent=filled に補正

broker には触れない (自動照合しない = スコープ境界)。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def recover_pending_intents(orch, *, now: datetime) -> dict[str, int]:
    """recovery 候補を 3 分岐で処理する。集計 dict を返す。"""
    summary = {"retryable": 0, "needs_reconcile": 0, "corrected_filled": 0}
    for intent in orch.get_stale_or_unconfirmed_intents(now=now):
        if intent.status == "pending":
            # 未送信クラッシュ: terminal 化して新 plan で再発注 (同一 plan は蘇生しない)。
            orch.set_recovery_status(plan_id=intent.plan_id, recovery_status="retryable")
            orch.record_order_result(plan_id=intent.plan_id, status="abandoned")
            orch.update_plan_status(intent.plan_id, "invalidated")
            summary["retryable"] += 1
        elif intent.status == "submitted" and intent.order_id is None:
            # 送信直後クラッシュ: 建玉あるかもしれない → 隔離 (terminal 化しない) + alert。
            orch.set_recovery_status(
                plan_id=intent.plan_id, recovery_status="needs_reconcile",
            )
            summary["needs_reconcile"] += 1
            logger.warning(
                "[ORCH-RECOVERY] needs_reconcile: plan %s submitted but order_id "
                "unknown — 再 trigger 禁止・要手動確認 (建玉照合が必要)", intent.plan_id,
            )
        elif intent.status == "submitted" and intent.order_id is not None:
            # 約定確定だが status 補正前: filled に補正 (plan は triggered のまま)。
            orch.record_order_result(
                plan_id=intent.plan_id, status="filled", order_id=intent.order_id,
            )
            summary["corrected_filled"] += 1
    if any(summary.values()):
        logger.info("[ORCH-RECOVERY] %s", summary)
    return summary
```

> `get_stale_or_unconfirmed_intents` (Task 3) が 3 ケースすべてを拾う前提。`needs_reconcile` のみ plan/intent を触らず隔離する (建玉照合まで宙吊り=安全側)。他 2 ケースは terminal 化。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_taskf_recovery_job.py -v`
Expected: PASS (3 passed)。

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
    """mode=live + AppConfig.mode=live: runtime に execution_broker + position_mgr が注入される。"""
    # orchestrator.mode=live + AppConfig.mode=live + live_broker 設定で build し、
    # rt._execution_broker is not None / rt._mode == "live" を確認。
    # create_broker / PositionManager は monkeypatch で軽量 stub に差し替え (本番接続しない)。
    ...


def test_live_mode_with_paper_injects_paper_broker(tmp_path, monkeypatch):
    """段階検証: orchestrator.mode=live + AppConfig.mode=paper でも execution_broker が
    注入される (paper_broker = 仮想資金で F 経路を動作確認、2026-06-25 改訂)。"""
    # orchestrator.mode=live + AppConfig.mode=paper (live_broker 不要) で build し、
    # rt._execution_broker is not None / rt._mode == "live" を確認。
    # create_broker は mode=paper で PaperBrokerAdapter を返す (monkeypatch で stub 化可)。
    ...


def test_live_test_requires_bridge_dry_run(tmp_path, monkeypatch):
    """codex Critical: orchestrator.mode=live + AppConfig.mode=live_test は、bridge /health の
    dry_run==true を確認できなければ起動を ValueError で弾く (実発注事故の防止)。"""
    # bridge /health の dry_run=false を返す stub で build → ValueError。
    # dry_run=true を返す stub なら build 成功 (execution_broker 注入)。
    # /health 取得失敗 (例外) でも ValueError (安全側)。
    ...
```

> 実装時に既存 `tests/test_orchestrator_bootstrap.py` の構築パターン (Phase 2/D の `test_producer_covers_all_tradeable...` 等) を流用。`create_broker` と `PositionManager`/`StateStore` を monkeypatch で stub 化し、本番 broker 接続を起こさない。bootstrap の execution 配線は `create_broker(mode=config.mode, ...)` を使うので、`orchestrator.mode=live` なら **top-level mode が paper でも live でも** execution broker が注入される (broker 種別は top-level mode が決める)。cross-field validation (Task 2) で live テストは AppConfig.mode=live + live_broker を満たす config を、paper 検証テストは AppConfig.mode=paper の config を作る。

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

        # codex Critical: live_test は ShadowBrokerAdapter の observer (MT5) が実 execute_signal
        # するため、bridge が DRY_RUN=false だと MT5 order_send に到達する。config では bridge の
        # runtime 状態を検証できないので、ここで bridge /health の dry_run を確認し、true でなければ
        # 起動を弾く (実発注事故の防止)。paper は MT5 に到達しないため gate 不要。
        if config.mode == "live_test":
            _assert_bridge_dry_run(config)  # /health の dry_run==true を要求。false/取得失敗で ValueError
```

> **`_assert_bridge_dry_run` (新規 helper):** bridge の `/health` (`mt5_bridge/server.py:217`、`HealthResponse.dry_run: bool`) を叩き、`dry_run==true` でなければ `ValueError`。取得失敗 (接続不可・タイムアウト) も安全側で `ValueError` に倒す (dry_run 状態を確認できないまま live_test を起動しない)。bridge URL / 認証ヘッダは既存 `MT5BridgeBroker` 構築と同じ config 経路を使う (実装時に確認)。この gate は `config.mode == "live_test"` かつ `orch_cfg.mode == "live"` のときのみ走る。

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

## Task 8.5: F-5 観測性 — plan 作成ログ + 約定成功ログ (spec §F-5, 2026-06-25 追加)

**Files:**
- Modify: `src/orchestrator/runtime.py` (`_notify_planning_result` 近辺で plan_create 成功時 INFO、執行段 `_execute_live_trigger` の executed 成功時 INFO)
- Test: `tests/test_taskf_observability_logs.py`

**Note:** spec §F-5。現状 plan 作成は DB / Discord にしか残らず、ターミナルログに出ない (`_notify_planning_result`, `runtime.py:183`)。一方 trigger は `🧪 shadow trigger` を INFO で出す (`runtime.py:611`)。この非対称で shadow/live 検証中に「いつ plan ができ、いつ trigger/execute したか」をログだけで追えない。発注 (executed) も `is_alertable_outcome("executed")==False` のため warning が出ない。**mode 非依存** (shadow/live 両方で有効) の観測性改善。

- [ ] **Step 1: Write the failing test**

`tests/test_taskf_observability_logs.py` を新規作成:

```python
import logging

import pytest

# plan_create 成功時に [ORCH] 📋 plan created INFO が 1 本出る (shadow trigger 🧪 と対)。
# direct_hold / failed には出さない。実装時に既存 runtime テストの fixture/モック構築に倣う。
# (PipelineResult(outcome="plan_create", plan_id=N, direction=..., score=..., confidence=...)
#  を返す pipeline をモックし、_notify_planning_result 経路を通す。)


@pytest.mark.asyncio
async def test_plan_create_emits_info_log(caplog):
    """plan_create 成功時に [ORCH] 📋 plan created ... INFO が 1 本出る。"""
    # 実装時: outcome="plan_create" の PipelineResult を返す runtime を組み、
    # run_planning_cycle 実行 → caplog に "📋 plan created" の INFO が 1 件あること。
    ...


@pytest.mark.asyncio
async def test_direct_hold_emits_no_plan_created_log(caplog):
    """direct_hold では 📋 plan created を出さない (既存通知方針と同じ)。"""
    ...


@pytest.mark.asyncio
async def test_live_execute_success_emits_info_log(caplog):
    """executed (約定成功) 時に [ORCH] ✅ live execute ... INFO が 1 本出る。

    executed は is_alertable_outcome==False で warning が出ないため、別途 INFO を足す。
    """
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taskf_observability_logs.py -v`
Expected: FAIL — plan created / live execute success の INFO がまだ無い。

- [ ] **Step 3: Write minimal implementation (plan created ログ)**

`src/orchestrator/runtime.py` の `_notify_planning_result` (`runtime.py:183` 近辺) で、plan_create 成功時 (Discord 通知の手前/直後、deterministic path 外) に INFO を 1 本追加:

```python
        # F-5 (spec §F-5): plan 作成成功をターミナルログにも出す (shadow trigger 🧪 と対)。
        # plan_create のときのみ。direct_hold / failed は出さない (既存通知方針と同じ)。
        if result.outcome == "plan_create" and result.plan_id is not None:
            logger.info(
                "[ORCH] 📋 plan created %s %s %s score=%+.2f conf=%.2f",
                result.plan_id, pair, result.direction or "?",
                result.score if result.score is not None else 0.0,
                result.confidence if result.confidence is not None else 0.0,
            )
```

> 配置は `_notify_planning_result` の plan_create 分岐 (notify_plan_created 呼出と同じ条件) に合わせる。reject も任意で 1 行 (`[ORCH] plan rejected %s: %s`) を足してよい (実装判断)。

- [ ] **Step 4: Write minimal implementation (約定成功ログ)**

`_execute_live_trigger` (Task 4 で新設) の executed 成功時に INFO を 1 本追加 (体裁は 🧪 shadow trigger と揃える):

```python
        # F-5: executed は is_alertable_outcome==False で warning が出ないため INFO を足す。
        # 注意: ExecutionResult に order_id 属性は無い (broker_adapter.py:34 は order: Order|None)。
        # order_id は result.order.order_id から取る (codex Medium)。
        if result.outcome == "executed":
            order_id = result.order.order_id if result.order is not None else None
            logger.info(
                "[ORCH] ✅ live execute plan %s %s @ %s",
                plan.plan_id, pair, order_id,
            )
```

> Task 4 の execute 結果反映 (§2 step6) の直後に置く。Task 4 実装時にこの 1 行を含めてもよい (その場合 Task 8.5 はテストのみ)。`ExecutionResult` は `order: Order | None` を持つ (`broker_adapter.py:34`)。`order_id` という直属属性は無いので `result.order.order_id if result.order else None` を使う。

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_taskf_observability_logs.py -v`
Expected: PASS。`📋 plan created` / `✅ live execute` が出て、direct_hold には出ない。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_taskf_observability_logs.py
git commit -m "feat: F-5 plan作成・約定成功のターミナルログ (plan→trigger→execute 可観測化)"
```

---

## Task 9: settings.yaml.example + 全体回帰 + Review Checklist

**Files:**
- Modify: `config/settings.yaml.example` (orchestrator.mode / execution_opinion_recheck_enabled のコメント)

- [ ] **Step 1: settings.yaml.example に記載**

`config/settings.yaml.example` の orchestrator セクション (`mode:` の近く) にコメント追記:

```yaml
  # Task F: shadow→本番発注 昇格。live は orchestrator 執行段が単一発注主体になる。
  # 発注先 broker は top-level mode (paper/live/live_test) が決める:
  #   - mode=live + AppConfig.mode=paper    → paper_broker (仮想資金) で動作確認 (推奨の段階検証)
  #   - mode=live + AppConfig.mode=live_test → paper + MT5 observer (実発注なし) で検証
  #   - mode=live + AppConfig.mode=live     → 本番発注 (live_broker 必須、未設定は起動時エラー)
  mode: "shadow"  # shadow | live
  # 発注直前の ExecutionOpinion 再点火 (material change 時)。既定 OFF (決定的・高速執行)。
  execution_opinion_recheck_enabled: false
```

- [ ] **Step 2: 全 F テスト**

Run: `pytest tests/test_taskf_order_intent_status_map.py tests/test_taskf_config_validation.py tests/test_taskf_recovery_query.py tests/test_taskf_execute_live_trigger.py tests/test_taskf_reject_transitions.py tests/test_taskf_recovery_job.py tests/test_taskf_single_writer_guard.py tests/test_taskf_bootstrap_wiring.py tests/test_taskf_observability_logs.py -v`
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
- F-5 plan 作成成功 INFO (📋 plan created) / 約定成功 INFO (✅ live execute) / direct_hold・failed には出さない → Task 8.5

- [ ] **Step 5: Commit**

```bash
git add config/settings.yaml.example
git commit -m "docs: settings.yaml.example に orchestrator.mode=live (Task F) 追記"
```

---

## 完了条件

- `OrchestratorConfig.mode=shadow` (既定) で全既存挙動が無改変 (回帰グリーン)。
- `mode=live` で orchestrator 執行段が trigger→final gate→execute を single writer 実行。発注先 broker は top-level `AppConfig.mode` が決める: `paper`/`live_test` なら仮想資金/observer で動作確認 (段階検証)、`live`+`live_broker` で本番発注。
- cross-field validation は `AppConfig.mode=live` のときだけ `live_broker` 必須を課し、`orchestrator.mode=live`+`AppConfig.mode=paper`/`live_test` (段階的 paper 検証) は通す (2026-06-25 改訂)。
- order_intents UNIQUE で二重発注を防ぎ、起動時 recovery が pending→retryable / submitted+null→needs_reconcile を分類 (送信直後クラッシュを取りこぼさない)。
- gate reject / 非 executed 後に plan が invalidated + intent terminal 化し、再発注は新 plan_id (replan) で行うため永久ブロックしない。
- 旧 trading cycle の新規 entry が全 entry point (main/API/CLI/TUI) で停止 (single writer)。
- F-5 観測性: plan 作成成功で `📋 plan created`、約定成功で `✅ live execute` の INFO がターミナルログに出る (shadow trigger 🧪 と対)。direct_hold / failed には出さない。shadow/live 両方で有効 (mode 非依存)。
- 旧経路コード削除 (omit) / needs_reconcile 自動照合 / material recheck ON 化は本 plan のスコープ外 (将来課題)。
