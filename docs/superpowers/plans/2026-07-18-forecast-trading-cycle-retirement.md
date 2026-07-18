# forecast/取引サイクル退役 + reflection job 新設 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** forecast サイクルと取引サイクルを完全削除し、発注を orchestrator に一本化。決済後 LLM 振り返りだけを新規 reflection job として再設計し、fail-fast 起動と migration を整備する。

**Architecture:** 先に新規基盤 (reflections テーブル / strict reflector / RAG filter / reflection job) を TDD で作り、その後に forecast 系 → 取引サイクル系の順で削除、最後に main.py の fail-fast 再構成・migration スクリプト・discord_bot 追従を行う。削除前に新基盤を完成させることで、各タスク終了時に suite green を維持する。

**Tech Stack:** Python / SQLAlchemy ORM (`_Base` + `metadata.create_all`) / ChromaDB / schedule ライブラリ / pytest。

**Spec:** `docs/superpowers/specs/2026-07-18-forecast-trading-cycle-retirement-design.md` (必読。§ 参照は spec の章番号)

---

## 実行環境の注意 (全タスク共通)

- **finance の uv/pytest は必ず WSL 内で実行する**。Windows 側 UNC 経由の `uv run` は `.venv` を破壊する:
  ```bash
  wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_xxx.py -x -q"
  ```
- ブランチ: `feat/orchestrator-observability` から `feat/cycle-retirement` を切って作業する
  (spec/plan コミットを含む最新ブランチ。main には未マージの前提)。
- コミットメッセージは conventional commits (日本語可)。attribution なし。
- 回帰基準: 作業中は per-file green、**最終合格基準は full suite `uv run pytest` で
  既知失敗のみ** (`tests/test_insights.py` ChromaDB 系 2 件 — CLAUDE.md 基準)。
- 実行順序は Task 番号順 (0→1→…→11)。fail-fast (Task 7) が削除 (Task 8) より
  先なのは意図的 (中間コミットの起動安全性)。

## ファイル構成 (最終形)

| ファイル | 責務 | 扱い |
|---|---|---|
| `src/cycles/reflection.py` | **新規**: reflection job (検知/枠/retry/controller) | 作成 |
| `src/data/orchestrator_store.py` | `_Reflection` model + retry CRUD + 逆引き + planning 照会 | 追記 |
| `src/analysis/reflector.py` | strict 化・簡素化 (fallback/adaptive 提案削除) | 修正 |
| `src/rag/directional_writer.py` | `record_trade_complete` strict 化、forecast/hold/entry writer 削除 | 修正 |
| `src/rag/directional_store.py` | 複合 filter query + 退役カード削除 API | 修正 |
| `src/jobs/news_collector.py` | 教訓検索を trade+complete に限定 | 修正 |
| `src/cycles/forecast.py`, `src/cycles/trading.py` | — | **削除** |
| `src/analysis/forecaster.py` / `performance_audit.py` / `audit_post_hoc.py` / `audit_report.py` | — | **削除** |
| `src/signals/accuracy_tracker.py` / `rag_adjustment.py` | — | **削除** |
| `src/data/session_store.py` / `src/persistence/adaptive_params_store.py` | — | **削除** |
| `src/trading/atr_calculator.py` | caller 全滅 (spec §2.2 caller sweep 帰結) | **削除** |
| `main.py` | fail-fast 再構成 + ジョブ登録整理 | 修正 |
| `scripts/migrate_cycle_retirement.py` | **新規**: 0 ベース migration | 作成 |
| discord_bot `cogs/finance/client.py` / `finance_cog.py` | forecast/run_trade 導線削除 | 修正 (別リポジトリ) |

---

### Task 0: ブランチ作成

- [ ] **Step 1: ブランチを切る**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git checkout feat/orchestrator-observability && git checkout -b feat/cycle-retirement && git log --oneline -1"
```

Expected: HEAD が spec 最新コミット (1185b89 以降)。

---

### Task 1: OrchestratorStore — `_Reflection` model + retry CRUD + 逆引き + planning 照会

**Files:**
- Modify: `src/data/orchestrator_store.py`
- Test: `tests/test_orchestrator_store_reflections.py` (新規)

spec: §3.2b (retry/dead/backoff)、§3.4 (テーブル定義)、§3.3 (`get_order_intent_by_order_id` + index)、§3.6 (`has_running_planning_run` / dangling 回収)。

- [ ] **Step 1: failing tests を書く**

`tests/test_orchestrator_store_reflections.py` を新規作成。既存 `tests/test_orchestrator_store.py` の fixture パターン (`OrchestratorStore(tmp_path / "orch.db")`) を踏襲:

```python
"""reflections テーブルと planning 照会 API のテスト (spec §3.2b/§3.4/§3.6)。"""
from datetime import timedelta

import pytest

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


@pytest.fixture
def store(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


NOW = db_now()


class TestReflectionCrud:
    def test_get_reflection_missing_returns_none(self, store):
        assert store.get_reflection("no-such-order") is None

    def test_mark_done_upserts_and_get_returns_done(self, store):
        store.mark_reflection_done(
            "ord-1", plan_id=7, pair="USDJPY=X", close_reason="take_profit",
            realized_pnl=120.0, reflection_text="lesson text",
            was_directionally_correct=True, now=NOW,
        )
        r = store.get_reflection("ord-1")
        assert r.status == "done"
        assert r.plan_id == 7
        assert r.was_directionally_correct is True
        assert r.reflection_text == "lesson text"

    def test_mark_done_allows_null_plan_id(self, store):
        store.mark_reflection_done(
            "ord-legacy", plan_id=None, pair="EURUSD=X", close_reason="manual",
            realized_pnl=-5.0, reflection_text="t",
            was_directionally_correct=False, now=NOW,
        )
        assert store.get_reflection("ord-legacy").plan_id is None

    def test_retry_increments_and_sets_backoff(self, store):
        store.mark_reflection_retry("ord-2", pair="USDJPY=X", error="LLM timeout", now=NOW)
        r = store.get_reflection("ord-2")
        assert r.status == "retry"
        assert r.attempt_count == 1
        assert r.last_error == "LLM timeout"
        assert r.next_retry_at == NOW + timedelta(hours=1)   # backoff[0]

    def test_backoff_progression_1_2_4_8(self, store):
        # 5 回目の失敗は dead になるため、backoff は 4 段 (1/2/4/8h) で全段使われる
        expected_hours = [1, 2, 4, 8]
        for i, h in enumerate(expected_hours, start=1):
            store.mark_reflection_retry("ord-3", pair="USDJPY=X", error="e", now=NOW)
            r = store.get_reflection("ord-3")
            assert r.status == "retry"
            assert r.attempt_count == i
            assert r.next_retry_at == NOW + timedelta(hours=h)

    def test_fifth_failure_becomes_dead(self, store):
        for _ in range(5):
            store.mark_reflection_retry("ord-4", pair="USDJPY=X", error="e", now=NOW)
        r = store.get_reflection("ord-4")
        assert r.status == "dead"
        assert r.attempt_count == 5

    def test_mark_dead_directly(self, store):
        store.mark_reflection_dead("ord-5", pair="XXXYYY=X",
                                   error="pair not in instruments", now=NOW)
        r = store.get_reflection("ord-5")
        assert r.status == "dead"
        assert "not in instruments" in r.last_error

    def test_retry_then_done_clears_error_state(self, store):
        store.mark_reflection_retry("ord-6", pair="USDJPY=X", error="e", now=NOW)
        store.mark_reflection_done(
            "ord-6", plan_id=None, pair="USDJPY=X", close_reason="stop_loss",
            realized_pnl=-30.0, reflection_text="t",
            was_directionally_correct=False, now=NOW,
        )
        r = store.get_reflection("ord-6")
        assert r.status == "done"
        assert r.last_error is None        # done 遷移で error 状態を消去
        assert r.next_retry_at is None

    def test_get_reflections_lists_all(self, store):
        store.mark_reflection_retry("a", pair="P", error="e", now=NOW)
        store.mark_reflection_dead("b", pair="P", error="e", now=NOW)
        ids = {r.order_id for r in store.get_reflections()}
        assert ids == {"a", "b"}


class TestOrderIntentLookup:
    def test_get_by_order_id(self, store):
        import sqlalchemy as sa
        # 現行シグネチャ (orchestrator_store.py:913): owner_run_id: int と
        # lease_until: datetime が keyword-only 必須
        owner = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                                trigger_type="watch_cycle")
        ok = store.try_insert_order_intent(
            plan_id=1, pair="USDJPY=X", intended_action="buy",
            owner_run_id=owner, lease_until=NOW + timedelta(seconds=60),
            trigger_id="t1", decision_id=None,
        )
        assert ok
        # order_id は broker 送信後に order worker がセットする運用カラム。
        # 逆引き getter だけが実装対象なので、テストでは生 UPDATE で再現する。
        with store._engine.connect() as conn:
            conn.execute(sa.text(
                "UPDATE order_intents SET order_id='broker-123' WHERE plan_id=1"))
            conn.commit()
        found = store.get_order_intent_by_order_id("broker-123")
        assert found is not None
        assert found.plan_id == 1

    def test_get_by_order_id_missing(self, store):
        assert store.get_order_intent_by_order_id("nope") is None


class TestPlanningRunQuery:
    def test_no_runs_returns_false(self, store):
        assert store.has_running_planning_run(now=NOW) is False

    def test_running_planning_run_returns_true(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="planning_cycle")
        assert store.has_running_planning_run(now=NOW) is True

    def test_finished_run_returns_false(self, store):
        rid = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                              trigger_type="planning_cycle")
        store.finish_run(rid)
        assert store.has_running_planning_run(now=NOW) is False

    def test_stale_run_excluded(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="planning_cycle")
        later = NOW + timedelta(seconds=601)
        assert store.has_running_planning_run(now=later) is False

    def test_other_trigger_types_ignored(self, store):
        store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                        trigger_type="watch_cycle")
        assert store.has_running_planning_run(now=NOW) is False

    def test_finish_dangling_runs(self, store):
        rid = store.start_run("OrchestratorRuntime", pair="USDJPY=X",
                              trigger_type="planning_cycle")
        n = store.finish_dangling_runs(now=NOW)
        assert n == 1
        run = store.get_run(rid)
        assert run.status == "failed"
        assert run.error_type == "dangling"
        assert run.finished_at is not None
```


- [ ] **Step 2: テストが fail することを確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_orchestrator_store_reflections.py -x -q 2>&1 | tail -5"
```

Expected: `AttributeError: ... has no attribute 'mark_reflection_done'` 等で FAIL。

- [ ] **Step 3: 実装**

`src/data/orchestrator_store.py` に追加。

(a) ORM model (`_OrderIntent` の後、`:150` 付近):

```python
class _Reflection(_Base):
    """決済済みトレードの LLM 振り返り + retry 管理 (spec §3.2b/§3.4)。"""
    __tablename__ = "reflections"

    order_id                  = Column(String, primary_key=True)
    plan_id                   = Column(Integer)          # NULL = orchestrator 経由でない決済
    pair                      = Column(String, nullable=False)
    close_reason              = Column(String)
    realized_pnl              = Column(Float)
    reflection_text           = Column(String)           # done 以外は NULL 可
    was_directionally_correct = Column(Boolean)          # done 時は必須 (§3.5b 機械判定)
    status                    = Column(String, nullable=False)   # done | retry | dead
    attempt_count             = Column(Integer, nullable=False, default=0)
    last_error                = Column(String)
    next_retry_at             = Column(DateTime)
    created_at                = Column(DateTime, nullable=False)
    updated_at                = Column(DateTime, nullable=False)
```

(b) モジュール定数 (class OrchestratorStore の直前):

```python
# reflection retry の指数 backoff (時間)。5 回目の失敗で dead になるため
# backoff は 4 段で全段使われる (spec §3.2b、plan レビューで 16h 到達不能を解消)。
_REFLECTION_BACKOFF_HOURS = (1, 2, 4, 8)
_REFLECTION_MAX_ATTEMPTS = 5
# planning 実行中判定の stale 閾値 (秒)。これより古い未完了 run は無視 (spec §3.6)。
_PLANNING_STALE_SECONDS = 600
```

(c) `_migrate()` の末尾に index 追加 (既存 ALTER ループの後):

```python
        # order_id 逆引き index (spec §3.3)。既存 DB にも冪等に張る。
        with self._engine.connect() as conn:
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_order_intents_order_id "
                    "ON order_intents(order_id)"
                ))
                conn.commit()
            except Exception:
                pass
```

(d) CRUD メソッド群 (既存 getter の `session.expunge()` 規約に従う):

```python
    # ── reflections (spec §3.2b/§3.4) ──────────────────────────

    def get_reflection(self, order_id: str) -> _Reflection | None:
        with Session(self._engine) as session:
            r = session.get(_Reflection, order_id)
            if r is not None:
                session.expunge(r)
            return r

    def get_reflections(self) -> list[_Reflection]:
        with Session(self._engine) as session:
            rows = list(session.execute(select(_Reflection)).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    def mark_reflection_done(
        self, order_id: str, *, plan_id: int | None, pair: str,
        close_reason: str | None, realized_pnl: float | None,
        reflection_text: str, was_directionally_correct: bool,
        now: datetime,
    ) -> None:
        """LLM + RAG 成功後にのみ呼ぶ (spec §3.5 不変条件)。"""
        with Session(self._engine) as session:
            r = session.get(_Reflection, order_id)
            if r is None:
                r = _Reflection(order_id=order_id, created_at=now,
                                attempt_count=0)
                session.add(r)
            r.plan_id = plan_id
            r.pair = pair
            r.close_reason = close_reason
            r.realized_pnl = realized_pnl
            r.reflection_text = reflection_text
            r.was_directionally_correct = was_directionally_correct
            r.status = "done"
            r.last_error = None       # retry からの遷移で error 状態を消去
            r.next_retry_at = None
            r.updated_at = now
            session.commit()

    def mark_reflection_retry(
        self, order_id: str, *, pair: str, error: str, now: datetime,
    ) -> None:
        """失敗を記録し backoff を進める。5 回目で dead に落ちる。"""
        with Session(self._engine) as session:
            r = session.get(_Reflection, order_id)
            if r is None:
                r = _Reflection(order_id=order_id, pair=pair,
                                created_at=now, attempt_count=0)
                session.add(r)
            r.attempt_count += 1
            r.last_error = error
            r.updated_at = now
            if r.attempt_count >= _REFLECTION_MAX_ATTEMPTS:
                r.status = "dead"
                r.next_retry_at = None
                logger.warning(
                    f"[REFLECT] {order_id} dead-lettered after "
                    f"{r.attempt_count} attempts: {error}")
            else:
                r.status = "retry"
                hours = _REFLECTION_BACKOFF_HOURS[
                    min(r.attempt_count - 1, len(_REFLECTION_BACKOFF_HOURS) - 1)]
                r.next_retry_at = now + timedelta(hours=hours)
            session.commit()

    def mark_reflection_dead(
        self, order_id: str, *, pair: str, error: str, now: datetime,
    ) -> None:
        """恒久不能 (instrument 不在等) を即 dead 記録する。"""
        with Session(self._engine) as session:
            r = session.get(_Reflection, order_id)
            if r is None:
                r = _Reflection(order_id=order_id, pair=pair,
                                created_at=now, attempt_count=0)
                session.add(r)
            r.status = "dead"
            r.last_error = error
            r.next_retry_at = None
            r.updated_at = now
            session.commit()

    # ── order_intents 逆引き (spec §3.3) ────────────────────────

    def get_order_intent_by_order_id(self, order_id: str) -> _OrderIntent | None:
        with Session(self._engine) as session:
            stmt = select(_OrderIntent).where(_OrderIntent.order_id == order_id)
            intent = session.execute(stmt).scalars().first()
            if intent is not None:
                session.expunge(intent)
            return intent

    # ── planning 実行中照会 (spec §3.6, best effort) ────────────

    def has_running_planning_run(self, *, now: datetime) -> bool:
        """実行中 planning run の有無。stale (>10 分) は実行中と見なさない。"""
        threshold = now - timedelta(seconds=_PLANNING_STALE_SECONDS)
        with Session(self._engine) as session:
            stmt = (
                select(_AgentRun.run_id)
                .where(_AgentRun.trigger_type == "planning_cycle")
                .where(_AgentRun.finished_at.is_(None))
                .where(_AgentRun.started_at > threshold)
                .limit(1)
            )
            return session.execute(stmt).first() is not None

    def finish_dangling_runs(self, *, now: datetime) -> int:
        """起動時に前プロセスの未完了 run を failed で回収する (spec §3.6)。"""
        with Session(self._engine) as session:
            stmt = (
                update(_AgentRun)
                .where(_AgentRun.finished_at.is_(None))
                .values(status="failed", error_type="dangling",
                        finished_at=now)
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount
```

import に `timedelta` (`from datetime import datetime, timedelta`)・`update`・`Boolean` が
なければ追加。

- [ ] **Step 4: green 確認 + 既存 store テスト回帰**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_orchestrator_store_reflections.py tests/test_orchestrator_store.py -q 2>&1 | tail -3"
```

Expected: 全 PASS。

- [ ] **Step 5: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add src/data/orchestrator_store.py tests/test_orchestrator_store_reflections.py && git commit -m 'feat(orchestrator): reflections テーブル + retry管理 + order_id逆引き + planning照会API'"
```

---

### Task 2: reflector strict 化 + `record_trade_complete` strict 化

**Files:**
- Modify: `src/analysis/reflector.py`
- Modify: `src/rag/directional_writer.py:71-113`
- Test: `tests/test_reflector_strict.py` (新規。旧 `tests/test_reflector.py` は Task 8 で削除)

spec: §3.3 (プロンプト簡素化)、§3.5 (fallback 削除・例外伝搬)、§3.5b (schema validation + 機械判定)。

- [ ] **Step 1: failing tests を書く**

`tests/test_reflector_strict.py`:

```python
"""strict 化した generate_close_reflection のテスト (spec §3.5/§3.5b)。"""
import json

import pytest

from src.analysis.reflector import ReflectionValidationError, generate_close_reflection
from src.config.schema import InstrumentConfig
from src.trading.position_manager import Order
from src.utils.clock import db_now


PAIR = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY")


def _order(direction="buy", entry=150.0, close=151.0, reason="take_profit"):
    return Order(
        order_id="o1", pair="USDJPY=X", direction=direction,
        entry_price=entry, stop_loss=entry - 1.0, take_profit=entry + 2.0,
        position_size=1000, status="closed",
        closed_at=db_now(), close_price=close, close_reason=reason,
        realized_pnl=(close - entry) * 1000 * (1 if direction == "buy" else -1),
        signal_reason="test entry",
    )


class _FakeLLM:
    def __init__(self, response: str | Exception):
        self._response = response

    async def chat(self, messages, temperature=0.0):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _valid_json(**overrides):
    data = {
        "outcome_summary": "TP hit",
        "lesson": "good entry",
        "was_directionally_correct": True,
        "confidence_assessment": "ok",
    }
    data.update(overrides)
    return json.dumps(data)


async def test_success_returns_reflection():
    r = await generate_close_reflection(
        pair_cfg=PAIR, order=_order(), llm=_FakeLLM(_valid_json()))
    assert r.outcome_summary == "TP hit"
    assert "Lesson: good entry" in r.full_text


async def test_llm_exception_propagates():
    with pytest.raises(RuntimeError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM(RuntimeError("timeout")))


async def test_invalid_json_raises():
    with pytest.raises(Exception):   # extract_json の parse 失敗が伝搬する
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM("not json at all"))


@pytest.mark.parametrize("missing", ["outcome_summary", "lesson", "was_directionally_correct"])
async def test_missing_required_key_raises(missing):
    data = json.loads(_valid_json())
    del data[missing]
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(), llm=_FakeLLM(json.dumps(data)))


async def test_wrong_type_raises():
    with pytest.raises(ReflectionValidationError):
        await generate_close_reflection(
            pair_cfg=PAIR, order=_order(),
            llm=_FakeLLM(_valid_json(was_directionally_correct="yes")))


class TestMachineDirectionJudgment:
    """方向正誤は価格方向の機械判定 (spec §3.5b)。LLM 申告は上書きされる。"""

    async def test_buy_close_above_entry_is_correct(self, caplog):
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 151.0, "manual"),
            llm=_FakeLLM(_valid_json(was_directionally_correct=False)))
        assert r.was_directionally_correct is True   # 機械判定が勝つ
        # LLM 申告との不一致は warning に残る (spec §3.5b「整合確認」)
        assert any("machine verdict" in rec.message for rec in caplog.records)
        # RAG カード本文は機械判定値を明記する
        assert "directionally_correct=True" in r.full_text

    async def test_buy_close_below_entry_is_incorrect(self):
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 149.0, "stop_loss"),
            llm=_FakeLLM(_valid_json(was_directionally_correct=True)))
        assert r.was_directionally_correct is False

    async def test_sell_close_below_entry_is_correct(self, caplog):
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("sell", 150.0, 149.0, "take_profit"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
        # 一致時は warning なし + full_text (RAG へ渡す本文) に機械判定値
        assert not any("machine verdict" in rec.message for rec in caplog.records)
        assert "directionally_correct=True" in r.full_text

    async def test_trailing_sl_with_profit_buy_is_correct(self):
        # 旧実装の won = (close_reason == "take_profit") では誤判定していたケース
        r = await generate_close_reflection(
            pair_cfg=PAIR, order=_order("buy", 150.0, 150.8, "profit_lock"),
            llm=_FakeLLM(_valid_json()))
        assert r.was_directionally_correct is True
```

`tests/test_directional_writer_strict.py` も新規:

```python
"""record_trade_complete の strict 化テスト (spec §3.5)。"""
import pytest

from src.rag.directional_writer import record_trade_complete


class _FailingStore:
    class directional:
        @staticmethod
        def upsert(**kwargs):
            raise RuntimeError("chroma down")


async def _embed(text):
    return [0.0] * 8


class _Order:
    order_id = "o1"; pair = "USDJPY=X"; direction = "buy"
    entry_price = 150.0; close_price = 151.0; close_reason = "take_profit"
    realized_pnl = 100.0; signal_reason = "r"


async def test_rag_failure_propagates():
    with pytest.raises(RuntimeError):
        await record_trade_complete(_FailingStore(), _embed, _Order(), "text")
```

- [ ] **Step 2: fail 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_reflector_strict.py tests/test_directional_writer_strict.py -x -q 2>&1 | tail -5"
```

Expected: `ImportError: cannot import name 'ReflectionValidationError'` 等で FAIL。

- [ ] **Step 3: 実装**

`src/analysis/reflector.py`:

1. `ReflectionValidationError(Exception)` を追加。
2. シグネチャ簡素化 — `macro_context_at_entry` / `sltp_comparison` / `param_history` 引数を削除
   (spec §3.3。文脈は `entry_analysis` = plan reasoning のみ):
   ```python
   async def generate_close_reflection(
       pair_cfg, order: "Order", llm: LLMClient, temperature: float = 0.1,
       user_notes: str = "", entry_analysis: str = "",
   ) -> Reflection:
   ```
   プロンプト組み立てから削除引数由来の section を除去する。
3. `Reflection.atr_params_suggestion` フィールドと、プロンプト内の
   `atr_params_suggestion` 出力指示 (`:81-85` 付近) を削除 (adaptive 退役)。
4. fallback 削除 (`:176-181` の try/except を除去):
   ```python
   text = await llm.chat(messages, temperature=temperature)
   data = extract_json(text)
   _REQUIRED = ("outcome_summary", "lesson", "was_directionally_correct")
   missing = [k for k in _REQUIRED if k not in data]
   if missing:
       raise ReflectionValidationError(f"missing keys: {missing}")
   if not isinstance(data["was_directionally_correct"], bool):
       raise ReflectionValidationError("was_directionally_correct must be bool")
   if not isinstance(data["outcome_summary"], str) or not isinstance(data["lesson"], str):
       raise ReflectionValidationError("outcome_summary/lesson must be str")
   ```
5. 機械判定 (`won = close_reason == "take_profit"` を置換):
   ```python
   # 方向正誤は価格方向の機械判定を正とする (spec §3.5b)。
   if order.direction == "buy":
       correct = close_price > order.entry_price
   else:
       correct = close_price < order.entry_price
   # LLM 申告は叙述の整合確認に使う (spec §3.5b)。不一致は lesson が逆方向の
   # 解釈を含む可能性があるため warning を残す。
   if data["was_directionally_correct"] != correct:
       logger.warning(
           f"[REFLECT] {order.order_id}: LLM directional claim "
           f"({data['was_directionally_correct']}) != machine verdict "
           f"({correct}) — using machine verdict")
   ```
   `Reflection(was_directionally_correct=correct, ...)` に機械判定を使い、
   **`full_text` にも機械判定値を明記する** (RAG カード本文が正誤の正を持つ):
   ```python
   full_text = (
       f"... | directionally_correct={correct} | Lesson: {lesson}"
   )   # 既存 full_text 組み立てに directionally_correct= を追加
   ```

`src/rag/directional_writer.py` の `record_trade_complete` (`:92-113`):
try/except を外し例外伝搬にする (embedding 生成 + upsert をベタに実行)。

- [ ] **Step 4: green 確認 + 既存呼び出し元の暫定追従**

`_finalize_closed_orders` (trading.py) は旧シグネチャで呼んでいるため、この時点で
trading 系テストが壊れる場合は **trading.py 側の呼び出しを新シグネチャに合わせる
最小修正** を入れる (Task 8 で削除されるまでの暫定):
`generate_close_reflection(pair_cfg=..., order=..., llm=..., temperature=...,
user_notes=..., entry_analysis=entry_analysis)` とし、adaptive 提案ブロック
(`reflection.atr_params_suggestion` 参照部, trading.py:348-364) を削除、
`_finalize_closed_orders` 全体を `try/except Exception: logger.warning` で包む
(strict 化で例外が漏れるため。旧挙動維持)。

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_reflector_strict.py tests/test_directional_writer_strict.py tests/test_reflector.py -q 2>&1 | tail -3"
```

旧 `tests/test_reflector.py` が fallback 前提で fail する場合は、このタスクで削除する
(spec §6: 再設計後は新規テストに置換)。

- [ ] **Step 5: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add -A src/analysis/reflector.py src/rag/directional_writer.py src/cycles/trading.py tests/ && git commit -m 'feat(reflect): reflector strict化 (fallback削除/schema validation/価格方向の機械判定) + record_trade_complete例外伝搬'"
```

---

### Task 3: DirectionalStore — 複合 filter query + 退役カード削除 API

**Files:**
- Modify: `src/rag/directional_store.py:94-129`
- Modify: `src/jobs/news_collector.py:135-136`
- Test: `tests/test_directional_store_filters.py` (新規)

spec: §3.4b。

- [ ] **Step 1: failing tests を書く**

`tests/test_directional_store_filters.py`:

```python
"""複合 filter と退役カード削除のテスト (spec §3.4b / 再レビュー Medium-5)。"""
import pytest

from src.rag.directional_store import DirectionalStore


@pytest.fixture
def store(tmp_path):
    return DirectionalStore(tmp_path / "chroma")


def _put(store, entry_id, session_type, phase):
    store.upsert(
        entry_id=entry_id, text=f"card {entry_id}", embedding=[0.1] * 8,
        direction="bullish", pair="USDJPY=X", session_id=entry_id,
        session_type=session_type, phase=phase,
        signal_score=0.0, confidence=0.0,
    )


@pytest.fixture
def seeded(store):
    _put(store, "t-complete", "trade", "complete")
    _put(store, "t-entry", "trade", "entry")
    _put(store, "f-entry", "forecast", "entry")
    _put(store, "f-complete", "forecast", "complete")
    _put(store, "h-review", "hold", "complete")
    return store


def test_combined_filter_returns_only_trade_complete(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10,
                        phase_filter="complete", session_type_filter="trade")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}


def test_phase_only_filter_unchanged(seeded):
    hits = seeded.query([0.1] * 8, "bullish", top_k=10, phase_filter="complete")
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete", "f-complete", "h-review"}


def test_cleanup_deletes_only_retired_cards(seeded):
    deleted = seeded.delete_retired_cards()
    assert deleted["bullish"] == 4       # t-entry, f-entry, f-complete, h-review
    hits = seeded.query([0.1] * 8, "bullish", top_k=10)
    ids = {h["metadata"]["session_id"] for h in hits}
    assert ids == {"t-complete"}


def test_cleanup_idempotent(seeded):
    seeded.delete_retired_cards()
    deleted_again = seeded.delete_retired_cards()
    assert deleted_again == {"bullish": 0, "bearish": 0}


def test_trade_complete_survives_cleanup(seeded):
    seeded.delete_retired_cards()
    assert seeded.count("bullish") == 1
```

- [ ] **Step 2: fail 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_directional_store_filters.py -x -q 2>&1 | tail -5"
```

Expected: `TypeError: query() got an unexpected keyword argument 'session_type_filter'` で FAIL。

- [ ] **Step 3: 実装**

`src/rag/directional_store.py`:

`query` の where 構築 (`:106-108`) を置換:

```python
    def query(
        self,
        query_embedding: list[float],
        direction: str,
        top_k: int = 5,
        phase_filter: str | None = None,
        session_type_filter: str | None = None,
    ) -> list[dict]:
        ...
        clauses = []
        if phase_filter:
            clauses.append({"phase": {"$eq": phase_filter}})
        if session_type_filter:
            clauses.append({"session_type": {"$eq": session_type_filter}})
        if len(clauses) == 1:
            where = clauses[0]
        elif clauses:
            where = {"$and": clauses}
        else:
            where = None
```

新規メソッド:

```python
    def delete_retired_cards(self) -> dict[str, int]:
        """forecast/hold カードと trade entry カードを削除する (冪等、spec §3.4b)。"""
        counts: dict[str, int] = {}
        for direction in ("bullish", "bearish"):
            col = self._collection(direction)
            before = col.count()
            col.delete(where={"session_type": {"$in": ["forecast", "hold"]}})
            col.delete(where={"$and": [
                {"session_type": {"$eq": "trade"}},
                {"phase": {"$eq": "entry"}},
            ]})
            counts[direction] = before - col.count()
        return counts
```

`src/jobs/news_collector.py:135-136` を limit 付き検索に変更:

```python
        bullish_hits = store.directional.query(
            query_embedding, "bullish", top_k=3,
            phase_filter="complete", session_type_filter="trade")
        bearish_hits = store.directional.query(
            query_embedding, "bearish", top_k=3,
            phase_filter="complete", session_type_filter="trade")
```

- [ ] **Step 4: green 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_directional_store_filters.py tests/test_integration_directional.py tests/test_directional_writer_horizon.py -q 2>&1 | tail -3"
```

- [ ] **Step 5: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add src/rag/directional_store.py src/jobs/news_collector.py tests/test_directional_store_filters.py && git commit -m 'feat(rag): directional query複合filter + 退役カード削除API + news教訓検索をtrade completeに限定'"
```

---

### Task 4: reflection job 本体 (`src/cycles/reflection.py`)

**Files:**
- Create: `src/cycles/reflection.py`
- Test: `tests/test_reflection_cycle.py` (新規)

spec: §3.2 (検知)、§3.2b (枠/retry)、§3.3 (plan 文脈)、§3.5 (失敗時)、§3.6 (controller/slot/planning 譲り)。

- [ ] **Step 1: failing tests を書く**

`tests/test_reflection_cycle.py`。fake slot / fake orch_store / trades.json を tmp_path に
組んでテストする:

```python
"""reflection job のテスト (spec §3.2/§3.2b/§3.3/§3.5/§3.6)。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.cycles.reflection import _select_targets, run_reflection_cycle
from src.data.orchestrator_store import OrchestratorStore
from src.trading.position_manager import Order
from src.utils.clock import db_now


NOW = db_now()


def _closed_order(oid, pair="USDJPY=X", closed_at=None):
    return Order(
        order_id=oid, pair=pair, direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1000,
        status="closed", closed_at=closed_at or NOW,
        close_price=151.0, close_reason="take_profit", realized_pnl=100.0,
        signal_reason="test",
    )


@pytest.fixture
def orch(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


class TestSelectTargets:
    def test_untried_newest_first_quota_2(self, orch):
        orders = [_closed_order(f"o{i}", closed_at=NOW + timedelta(hours=i))
                  for i in range(12)]
        targets = _select_targets(orders, orch, NOW)
        # 枠 10: 未試行の新しい順 2 (o11, o10) + 残り古い順 8 (o0..o7)
        ids = [t.order_id for t in targets]
        assert ids[:2] == ["o11", "o10"]
        assert ids[2:] == [f"o{i}" for i in range(8)]

    def test_done_and_dead_excluded(self, orch):
        orch.mark_reflection_done("a", plan_id=None, pair="P", close_reason="c",
                                  realized_pnl=0.0, reflection_text="t",
                                  was_directionally_correct=True, now=NOW)
        orch.mark_reflection_dead("b", pair="P", error="e", now=NOW)
        orders = [_closed_order("a"), _closed_order("b"), _closed_order("c")]
        ids = [t.order_id for t in _select_targets(orders, orch, NOW)]
        assert ids == ["c"]

    def test_retry_not_due_excluded_due_included(self, orch):
        orch.mark_reflection_retry("r1", pair="P", error="e", now=NOW)  # +1h
        orders = [_closed_order("r1")]
        assert _select_targets(orders, orch, NOW) == []
        later = NOW + timedelta(hours=1, minutes=1)
        ids = [t.order_id for t in _select_targets(orders, orch, later)]
        assert ids == ["r1"]

    def test_quota_spillover_when_few_untried(self, orch):
        # 未試行 1 件だけなら backfill に枠を融通し合計 10 まで
        orch.mark_reflection_retry("r1", pair="P", error="e", now=NOW - timedelta(hours=2))
        orders = [_closed_order("r1"), _closed_order("new1")]
        ids = {t.order_id for t in _select_targets(orders, orch, NOW)}
        assert ids == {"r1", "new1"}


class _FakeSlot:
    def __init__(self, busy_after=None, waiting=False):
        self.calls = 0
        self._busy_after = busy_after
        self.waiting_user_job = waiting

    def try_run_scheduled(self, fn, *args, **kwargs):
        if self._busy_after is not None and self.calls >= self._busy_after:
            return False
        self.calls += 1
        fn(*args, **kwargs)
        return True


def _config(tmp_path):
    """テスト用の最小 config another。実装の consumer 面に合わせ SimpleNamespace で足りる。"""
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        tradeable_instruments=[SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY")],
        llm=SimpleNamespace(reflection=SimpleNamespace(temperature=0.3)),
        orchestrator=SimpleNamespace(policy=SimpleNamespace(trade_horizon="swing")),
        user_notes_path=tmp_path / "notes.md",
    )


def _write_trades(tmp_path, orders):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "trades.json").write_text(
        json.dumps([o.to_dict() for o in orders]), encoding="utf-8")


class TestRunReflectionCycle:
    def test_processes_and_marks_done(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        processed = []

        async def fake_reflect_and_record(config, store, orch_store, llm,
                                          embed_fn, order, plan_id, entry_analysis):
            processed.append(order.order_id)
            return ("text", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record",
                            fake_reflect_and_record)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert processed == ["o1"]
        assert orch.get_reflection("o1").status == "done"

    def test_unknown_pair_dead_without_llm(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("ox", pair="GONE=X")])
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("ox").status == "dead"

    def test_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])

        async def boom(*args, **kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", boom)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        r = orch.get_reflection("o1")
        assert r.status == "retry"
        assert r.attempt_count == 1

    def test_slot_busy_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1"), _closed_order("o2")])
        slot = _FakeSlot(busy_after=1)
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       plan_id, entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert len(seen) == 1   # 2 件目で busy → controller 終了

    def test_waiting_user_job_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        slot = _FakeSlot(waiting=True)
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert slot.calls == 0

    def test_running_planning_stops_controller(self, tmp_path, orch, monkeypatch):
        _write_trades(tmp_path, [_closed_order("o1")])
        orch.start_run("OrchestratorRuntime", pair="USDJPY=X",
                       trigger_type="planning_cycle")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        slot = _FakeSlot()
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch, slot=slot)
        assert slot.calls == 0

    def test_trades_json_missing_is_noop(self, tmp_path, orch, monkeypatch):
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())   # 例外なく終了

    def test_broken_row_does_not_block_valid_rows(self, tmp_path, orch, monkeypatch):
        """不正行混在でも正常行は処理される (plan レビュー Medium-2)。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        good = _closed_order("good-1").to_dict()
        broken = {"order_id": "bad-1", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}   # from_dict が落ちる行
        no_id = {"pair": "USDJPY=X"}               # order_id なし不正行
        (state / "trades.json").write_text(
            json.dumps([broken, no_id, good]), encoding="utf-8")
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       plan_id, entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["good-1"]                        # 正常行は処理
        r = orch.get_reflection("bad-1")
        assert r.status == "retry"                       # 壊れ行は retry (即 dead にしない)
        assert "parse_error" in r.last_error

    def test_parse_retry_recovers_after_fix(self, tmp_path, orch, monkeypatch):
        """parser/データ修正後、retry 行は backoff 到来で正常処理される。"""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        # 1 回目: 壊れ行 → retry
        broken = {"order_id": "fix-1", "pair": "USDJPY=X",
                  "opened_at": "not-a-datetime"}
        (state / "trades.json").write_text(json.dumps([broken]), encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert orch.get_reflection("fix-1").status == "retry"
        # 2 回目: データ修正 + backoff 経過相当 → 処理されて done
        (state / "trades.json").write_text(
            json.dumps([_closed_order("fix-1").to_dict()]), encoding="utf-8")
        with orch._engine.connect() as conn:
            import sqlalchemy as sa
            conn.execute(sa.text(
                "UPDATE reflections SET next_retry_at = '2000-01-01 00:00:00' "
                "WHERE order_id='fix-1'"))
            conn.commit()
        seen = []

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       plan_id, entry_analysis):
            seen.append(order.order_id)
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert seen == ["fix-1"]
        assert orch.get_reflection("fix-1").status == "done"

    def test_trades_json_not_a_list_is_noop(self, tmp_path, orch, monkeypatch):
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "trades.json").write_text('{"oops": true}', encoding="utf-8")
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())   # warning のみで終了

    def test_plan_context_passed(self, tmp_path, orch, monkeypatch):
        """order_intent → plan_id → reasoning が _reflect_and_record に渡る。"""
        _write_trades(tmp_path, [_closed_order("bro-1")])
        # order_intent を仕込む (order_id="bro-1", plan_id=42)。
        # 現行シグネチャ: owner_run_id: int + lease_until: datetime 必須
        import sqlalchemy as sa
        owner = orch.start_run("OrchestratorRuntime", pair="USDJPY=X",
                               trigger_type="watch_cycle")
        orch.try_insert_order_intent(
            plan_id=42, pair="USDJPY=X", intended_action="buy",
            owner_run_id=owner, lease_until=NOW + timedelta(seconds=60),
            trigger_id="t", decision_id=None)
        with orch._engine.connect() as conn:
            conn.execute(sa.text(
                "UPDATE order_intents SET order_id='bro-1' WHERE plan_id=42"))
            conn.commit()
        # reasoning getter は record_decision の仕込みが重いので instance mock で固定
        monkeypatch.setattr(orch, "get_latest_plan_create_reasoning",
                            lambda pid: f"reasoning-for-{pid}")
        got = {}

        async def fake(config, store, orch_store, llm, embed_fn, order,
                       plan_id, entry_analysis):
            got["plan_id"] = plan_id
            got["entry_analysis"] = entry_analysis
            return ("t", True)

        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", fake)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert got["plan_id"] == 42
        assert got["entry_analysis"] == "reasoning-for-42"   # 実文字列が渡る

    def test_context_lookup_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        """plan 文脈取得の失敗も 1 件単位の例外境界に入る (レビュー Medium-4)。"""
        _write_trades(tmp_path, [_closed_order("o1")])
        def boom(order_id):
            raise RuntimeError("db locked")
        monkeypatch.setattr(orch, "get_order_intent_by_order_id", boom)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        # controller は例外を漏らさず終了し、retry 行が残る
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        r = orch.get_reflection("o1")
        assert r.status == "retry"

    def test_done_save_failure_marks_retry(self, tmp_path, orch, monkeypatch):
        """RAG 成功後の done 保存失敗 → retry (次回 RAG upsert は冪等)。"""
        _write_trades(tmp_path, [_closed_order("o1")])

        async def ok(config, store, orch_store, llm, embed_fn, order,
                     plan_id, entry_analysis):
            return ("t", True)

        calls = {"n": 0}
        real_done = orch.mark_reflection_done
        def flaky_done(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("db locked")
        monkeypatch.setattr(orch, "mark_reflection_done", flaky_done)
        monkeypatch.setattr("src.cycles.reflection._reflect_and_record", ok)
        monkeypatch.setattr("src.cycles.reflection.create_llm_client",
                            lambda config, role: object())
        monkeypatch.setattr("src.cycles.reflection.make_embed_fn",
                            lambda config: object())
        run_reflection_cycle(_config(tmp_path), store=None, orch_store=orch,
                             slot=_FakeSlot())
        assert calls["n"] == 1
        r = orch.get_reflection("o1")
        assert r.status == "retry"      # 処理済みにはならない → 次回再処理
```


- [ ] **Step 2: fail 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_reflection_cycle.py -x -q 2>&1 | tail -5"
```

Expected: `ModuleNotFoundError: No module named 'src.cycles.reflection'`。

- [ ] **Step 3: 実装 — `src/cycles/reflection.py`**

```python
"""reflection job — 決済済みトレードの LLM 振り返り (spec §3)。

trades.json の closed trades と reflections テーブルの差分から未振り返りを検知し、
1 件ずつ LLM slot 経由で振り返りを生成する。出力は reflections テーブル (done 記録)
と directional RAG の trade complete カード (news_collector への教訓供給)。

実行制御 (spec §3.6): JobGuard 配下の controller が同期実行され、各件の前に
slot busy / waiting_user_job / planning 実行中を確認して譲る。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from src.analysis.price_analyzer import load_user_notes   # 現行定義元 (price_analyzer.py:30)
from src.analysis.reflector import generate_close_reflection
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.directional_writer import record_trade_complete
from src.rag.embedder import make_embed_fn
from src.trading.position_manager import Order
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.data.orchestrator_store import OrchestratorStore

logger = logging.getLogger(__name__)

_NEW_QUOTA = 2      # 未試行 (新しい順) の優先枠 (spec §3.2b)
_TOTAL_QUOTA = 10   # 1 回の実行枠


def _parse_closed_trades(raw, orch_store, now: datetime) -> list[Order]:
    """trades.json の生 dict 群を行単位で Order に変換する。

    不正行が 1 つあっても他の行の処理を止めない (plan レビュー Medium-2)。
    order_id を持つ壊れ行は retry(parse_error) で記録する — デシリアライズ不具合
    (新フィールド追従漏れ等) は parser 修正で直り得るため即 dead にしない
    (即 dead は spec 上 instrument 不在のみ)。恒久的に壊れた行は backoff の末
    5 回で自然に dead へ落ちる。
    """
    if not isinstance(raw, list):
        logger.warning(f"[REFLECT] trades.json is not a list "
                       f"({type(raw).__name__}) — skipped")
        return []
    orders: list[Order] = []
    for d in raw:
        if not isinstance(d, dict):
            logger.warning(f"[REFLECT] non-dict trade row skipped: {d!r:.80}")
            continue
        oid = d.get("order_id")
        try:
            order = Order.from_dict(dict(d))
        except Exception as e:
            if oid:
                try:
                    orch_store.mark_reflection_retry(
                        oid, pair=str(d.get("pair", "?")),
                        error=f"parse_error: {type(e).__name__}: {e}", now=now)
                except Exception:
                    logger.exception(
                        f"[REFLECT] {oid}: failed to record parse error")
            logger.warning(
                f"[REFLECT] broken trade row skipped (order_id={oid}): {e}")
            continue
        if order.status == "closed":
            orders.append(order)
    return orders


def _select_targets(
    closed: list[Order], orch_store: "OrchestratorStore", now: datetime,
) -> list[Order]:
    """処理対象を枠規則で選ぶ (spec §3.2b)。

    eligible = 未試行 ∪ next_retry_at 到来済み retry。
    枠 10 = 未試行の新しい順 2 + 残り eligible の古い順 8 (融通あり)。
    """
    states = {r.order_id: r for r in orch_store.get_reflections()}

    def _eligible(o: Order) -> bool:
        r = states.get(o.order_id)
        if r is None:
            return True
        if r.status in ("done", "dead"):
            return False
        return r.next_retry_at is not None and r.next_retry_at <= now

    def _ts(o: Order) -> datetime:
        return o.closed_at or o.opened_at

    eligible = [o for o in closed if _eligible(o)]
    untried = sorted(
        (o for o in eligible if o.order_id not in states),
        key=_ts, reverse=True,
    )
    fresh = untried[:_NEW_QUOTA]
    fresh_ids = {o.order_id for o in fresh}
    backfill = sorted(
        (o for o in eligible if o.order_id not in fresh_ids), key=_ts,
    )
    return (fresh + backfill)[:_TOTAL_QUOTA]


async def _reflect_and_record(
    config, store, orch_store, llm, embed_fn, order: Order,
    plan_id: int | None, entry_analysis: str,
) -> tuple[str, bool]:
    """LLM 振り返り → RAG upsert。失敗は例外伝搬 (strict、spec §3.5)。"""
    pair_cfg = next(
        p for p in config.tradeable_instruments if p.symbol == order.pair)
    reflection = await generate_close_reflection(
        pair_cfg=pair_cfg, order=order, llm=llm,
        temperature=config.llm.reflection.temperature,
        user_notes=load_user_notes(config.user_notes_path, "reflect"),
        entry_analysis=entry_analysis,
    )
    await record_trade_complete(
        store, embed_fn, order, reflection.full_text,
        horizon=config.orchestrator.policy.trade_horizon,
    )
    return reflection.full_text, reflection.was_directionally_correct


def _process_one(config, store, orch_store, llm, embed_fn, order: Order) -> None:
    """1 件を処理する (slot 内で同期実行)。

    例外境界: instrument 判定より後の全処理 (文脈取得 / LLM / RAG / done 保存) を
    1 件単位の try に入れる (レビュー Medium-4)。失敗はこの中で retry 記録し、
    controller へは漏らさない。
    """
    now = db_now()
    pair_cfg = next(
        (p for p in config.tradeable_instruments if p.symbol == order.pair),
        None,
    )
    if pair_cfg is None:
        # 恒久不能: 現 instrument 設定に無い旧銘柄 → 即 dead (spec §3.2b)
        orch_store.mark_reflection_dead(
            order.order_id, pair=order.pair,
            error="pair not in tradeable instruments", now=now)
        logger.warning(
            f"[REFLECT] {order.order_id} ({order.pair}): pair not in "
            f"instruments — dead-lettered")
        return
    try:
        intent = orch_store.get_order_intent_by_order_id(order.order_id)
        plan_id = intent.plan_id if intent is not None else None
        entry_analysis = ""
        if plan_id is not None:
            entry_analysis = (
                orch_store.get_latest_plan_create_reasoning(plan_id) or "")
        text, correct = asyncio.run(_reflect_and_record(
            config, store, orch_store, llm, embed_fn, order,
            plan_id, entry_analysis))
        # done 保存の失敗も retry に落とす (RAG upsert は冪等なので次回無害)
        orch_store.mark_reflection_done(
            order.order_id, plan_id=plan_id, pair=order.pair,
            close_reason=order.close_reason, realized_pnl=order.realized_pnl,
            reflection_text=text, was_directionally_correct=correct,
            now=db_now())
    except Exception as e:
        try:
            orch_store.mark_reflection_retry(
                order.order_id, pair=order.pair,
                error=f"{type(e).__name__}: {e}", now=db_now())
        except Exception:
            # retry 記録すら失敗 (DB 断等) — ログのみ。未記録なので次回 未試行として再処理
            logger.exception(
                f"[REFLECT] {order.order_id}: failed to record retry state")
        logger.warning(
            f"[REFLECT] {order.order_id} failed ({type(e).__name__}: {e}) "
            f"— will retry")
        return
    logger.info(f"[REFLECT] {order.order_id} ({order.pair}) reflected "
                f"(directionally_correct={correct} plan_id={plan_id})")


def run_reflection_cycle(config, store, orch_store, *, slot) -> None:
    """JobGuard 配下で同期実行される controller (spec §3.6)。

    slot busy / waiting_user_job / planning 実行中のいずれかで残りを次回へ。
    """
    now = db_now()
    try:
        raw = StateStore(config.state_dir).load_trades_raw()
    except Exception:
        logger.warning("[REFLECT] trades.json read failed — skipped",
                       exc_info=True)
        return
    closed = _parse_closed_trades(raw, orch_store, now)
    targets = _select_targets(closed, orch_store, now)
    if not targets:
        return
    llm = create_llm_client(config, "reflection")
    embed_fn = make_embed_fn(config)
    for order in targets:
        if slot.waiting_user_job:
            logger.info("[REFLECT] user job waiting — yielding")
            break
        if orch_store.has_running_planning_run(now=db_now()):
            logger.info("[REFLECT] planning in progress — yielding")
            break
        ran = slot.try_run_scheduled(
            _process_one, config, store, orch_store, llm, embed_fn, order)
        if not ran:
            break   # slot busy — 残りは次回
```


- [ ] **Step 4: green 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_reflection_cycle.py tests/test_orchestrator_store_reflections.py -q 2>&1 | tail -3"
```

- [ ] **Step 5: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add src/cycles/reflection.py tests/test_reflection_cycle.py && git commit -m 'feat(reflect): reflection job本体 (検知/枠規則/retry/plan文脈/slot逐次controller)'"
```

---

### Task 5: bootstrap dangling 回収

**注意 (plan レビュー Medium-4)**: reflection job の **scheduler 登録はここでは行わない**。
旧 `_finalize_closed_orders` が残っている間に登録すると旧・新二経路で LLM reflection が
走るため、登録は旧取引サイクル削除と同一コミット (Task 8) で行う。

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (dangling 回収)
- Test: 既存 bootstrap テストに追記

- [ ] **Step 1: 実装**

`src/orchestrator/bootstrap.py` — `OrchestratorStore` 生成直後 (`:178` 付近) に:

```python
    # 前プロセスの dangling run を回収する (spec §3.6)
    recovered = orch_store.finish_dangling_runs(now=db_now())
    if recovered:
        logger.warning(f"[ORCH] recovered {recovered} dangling agent runs")
```

(`db_now` の import が bootstrap にあるか確認、なければ追加。)

- [ ] **Step 2: テスト**

既存 bootstrap テストに「起動時に dangling run が failed になる」ケースを追加:

```python
def test_bootstrap_recovers_dangling_runs(...):
    # OrchestratorStore に finished_at NULL の run を仕込む → build_orchestrator_runtime
    # (enabled=True の最小 config) → run が failed/dangling になる
```

- [ ] **Step 3: green 確認 + commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_reflection_cycle.py -q 2>&1 | tail -3 && git add -A src/orchestrator/bootstrap.py tests/ && git commit -m 'feat(orchestrator): 起動時のdangling run回収'"
```

---

### Task 6: forecast 系 完全削除

**Files (削除):**
- Delete: `src/cycles/forecast.py`、`src/analysis/forecaster.py`、`src/signals/accuracy_tracker.py`
- Modify: `src/data/analysis_store.py` (318-443 の `_ForecastRecord`/`ForecastStore` 削除)
- Modify: `src/signals/signal_combiner.py` (accuracy 分岐 101-144 / import :10-11 / 型 :16-17 / 引数 :52-53 / :190-191)
- Modify: `src/rag/directional_writer.py` (`record_forecast_entry` :119-156、`record_forecast_review` :159-194)
- Modify: `src/jobs/weekly_diagnosis.py` (`_build_accuracy_section` :90-114、:22 import、:204、forecast_store 引数 :91/:185/:272-275、:145-151/:163/:171-173 のテキスト)
- Modify: `src/rag/ask_context_builder.py` (`build_forecast_accuracy` :88-118、`_build_forecast_accuracy` :329-339、forecast_store 引数 :125/:130-131、:146-147/:153)
- Modify: `prompts/ask_user.j2` (:16-18 forecast_accuracy ブロック)
- Modify: `src/api/routes/data.py` (:137-165 `/forecast`)、`src/api/_state.py` (:33)、`src/api/server.py` (:69/:79/docstring)
- Modify: `src/views.py` (:22 import、:108-201 `run_forecast_view`、:216/:224)
- Modify: `src/cli.py` (:24/:35-36/:403-408 と `run_commands` の forecast_store 引数)
- Modify: `src/tui.py` (:59/:75/:178/:281/:301-306)
- Modify: `src/api/routes/health.py` (:284/:341-344/docstring)
- Modify: `main.py` (:36 import、:53 guard、:215、:308-322 表示、:407-416 登録、:471-472、:504-506、:589)
- Modify: `src/trading_cycle.py` (:15 forecast re-export)
- Modify: `src/notifications/notifier.py` (:106 `_SOURCE_LABELS["forecast"]`)
- Config: `src/config/schema.py` (:179-182/:185-199/:454-457)、`src/config/loader.py` (:47/:504-510/:608-611)、`config/settings.yaml.example` (:190-201/:303-306)
- Tests: `tests/test_accuracy_feedback.py` 削除。`tests/test_forecast*.py` があれば削除。
  `tests/test_ask_context_builder.py`/`tests/test_weekly_diagnosis.py`/
  `tests/test_directional_writer_horizon.py`/`tests/test_integration_directional.py`/
  `tests/test_config_loader.py`/`tests/test_config_example_sync.py` の該当部分を修正。

- [ ] **Step 1: 上記リストを順に削除・修正する**

方針:
- `combine_signals` は accuracy 引数と分岐だけ削除して温存 (`_summarize_pair` が使う)。
  :142-144 の `if accuracy_force_hold:` 削除に伴い :145 の `elif` を `if` に繰り上げ。
- `weekly_diagnosis` は forecast_store 引数を除去し、j2 テンプレート
  (`prompts/weekly_diagnosis_user.j2`) から `accuracy_section` 変数を除去。
- `ask_context_builder` の `build_trade_summary`/session 依存は **Task 8 で削除**
  (このタスクでは forecast 分のみ)。
- main.py の forecast_times/`_skipped_forecast` 変数群 (:308-322) と
  ジョブ登録 (:407-416) を削除。`run_times` 参照 (:316/:409-410) はここでは
  forecast フィルタ分のみ消え、`run_times` 本体は Task 8 で削除。

- [ ] **Step 2: 参照残りゼロ確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && grep -rn 'ForecastStore\|run_forecast_cycle\|forecast_cycle\|accuracy_tracker\|compute_recent_accuracy\|ForecastAccuracyFeedbackConfig\|forecast_accuracy_feedback\|record_forecast_entry\|record_forecast_review\|build_forecast_accuracy\|forecast_review_interval\|forecast_start_hour\|forecast_min_combined\|forecast_significance' src/ main.py prompts/ config/settings.yaml.example --include='*' | grep -v Binary"
```

Expected: ヒット 0 件。

- [ ] **Step 3: 影響 per-file テスト green 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_signal_combiner.py tests/test_weekly_diagnosis.py tests/test_ask_context_builder.py tests/test_config_loader.py tests/test_config_example_sync.py tests/test_main_wiring.py tests/test_directional_writer_horizon.py tests/test_integration_directional.py -q 2>&1 | tail -3"
```

(存在しないファイル名は実在に合わせ調整。)

- [ ] **Step 4: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add -A && git commit -m 'feat!: forecastサイクル完全削除 (cycle/store/accuracy/表示/API/config)'"
```

---

### Task 7: fail-fast 起動再構成 + pair 整合チェック

**Files:**
- Modify: `main.py` (起動順序: orchestrator 構築を API/scheduler より前へ、継続 guard 撤去)
- Modify: `src/orchestrator/bootstrap.py` (pair 整合チェック)
- Test: `tests/test_main_failfast.py` (新規) + bootstrap テスト追記

spec: §1.5。

- [ ] **Step 1: failing tests を書く**

bootstrap 側 (`tests/test_orchestrator_bootstrap*.py` 既存に追記 or 新規):

```python
def test_disabled_returns_none_unchanged(...):
    # enabled=False → None (従来通り。exit 判断は main 側)

def test_live_pair_subset_raises(...):
    # mode="live" かつ orch pairs が tradeable の真部分集合 → RuntimeError

def test_shadow_pair_subset_warns_but_builds(...):
    # mode="shadow" 同条件 → 構築成功 (warning のみ)
```

main 側 `tests/test_main_failfast.py` — main() を直接叩くのは重いので、
判定と起動順序をそれぞれ関数に切り出してテストする (レビュー Medium-5:
「build → validate → start → API → scheduler」の順序保証が中核):

```python
import pytest

from src.startup import ensure_orchestrator_or_exit, run_startup_sequence


class TestEnsureOrExit:
    def test_orchestrator_none_raises_system_exit(self):
        with pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=True)

    def test_disabled_raises_system_exit(self):
        with pytest.raises(SystemExit):
            ensure_orchestrator_or_exit(None, enabled=False)

    def test_shadow_mode_warns_not_exits(self, caplog):
        ensure_orchestrator_or_exit(object(), enabled=True, mode="shadow")
        assert any("発注は行われません" in r.message for r in caplog.records)


class TestStartupSequence:
    """build → validate → initialize → start → API → scheduler の順序保証。

    initialize (初回 news/tech 収集) は runtime.start() より前 (plan レビュー High-1:
    orchestrator の planning/watch loop は start() 直後から動くため、
    初回収集前の古い snapshot で判断・発注させない)。
    """

    def _spies(self):
        order = []
        runtime = type("R", (), {"start": lambda self: order.append("start")})()
        return order, {
            "build": lambda: (order.append("build"), runtime)[1],
            "validate": lambda rt: order.append("validate"),
            "initialize": lambda: order.append("init"),
            "start_api": lambda: order.append("api"),
            "start_scheduler": lambda: order.append("scheduler"),
        }

    def test_happy_path_order(self):
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order == ["build", "validate", "init", "start", "api", "scheduler"]

    def test_build_failure_stops_everything_after(self):
        order, fns = self._spies()
        def boom():
            order.append("build")
            raise RuntimeError("bootstrap failed")
        fns["build"] = boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build"]

    def test_validate_exit_stops_everything_after(self):
        order, fns = self._spies()
        def refuse(rt):
            order.append("validate")
            raise SystemExit(1)
        fns["validate"] = refuse
        with pytest.raises(SystemExit):
            run_startup_sequence(**fns)
        assert order == ["build", "validate"]

    def test_initialize_runs_before_runtime_start(self):
        order, fns = self._spies()
        run_startup_sequence(**fns)
        assert order.index("init") < order.index("start")

    def test_initialize_failure_stops_start_api_scheduler(self):
        order, fns = self._spies()
        def boom():
            order.append("init")
            raise RuntimeError("initial collection failed")
        fns["initialize"] = boom
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert order == ["build", "validate", "init"]   # start/api/scheduler なし

    def test_start_failure_stops_before_api_and_scheduler(self):
        order, fns = self._spies()
        runtime = type("R", (), {"start": lambda self: (_ for _ in ()).throw(
            RuntimeError("start failed"))})()
        fns["build"] = lambda: (order.append("build"), runtime)[1]
        with pytest.raises(RuntimeError):
            run_startup_sequence(**fns)
        assert "api" not in order and "scheduler" not in order
```

- [ ] **Step 2: 実装**

`src/orchestrator/bootstrap.py` — pairs 解決 (`:183-201`) の後に:

```python
    tradeable = {p.symbol for p in config.tradeable_instruments}
    selected = set(pairs)
    if selected != tradeable:
        missing = sorted(tradeable - selected)
        if orch_cfg.mode == "live":
            raise RuntimeError(
                f"[ORCH] pair set mismatch in live mode: {missing} have no "
                f"ordering path (spec §1.5). Fix orchestrator.pairs or "
                f"instruments config.")
        logger.warning(
            f"[ORCH] pair subset in {orch_cfg.mode} mode: {missing} not "
            f"covered (allowed outside live)")
```

`src/startup.py` (既存 startup_checks の隣) に新設:

```python
def ensure_orchestrator_or_exit(runtime, *, enabled: bool, mode: str = "live") -> None:
    """発注経路の存在を保証する (spec §1.5)。無ければ起動中止。"""
    if not enabled:
        _console.print("[red][FATAL] orchestrator.enabled=false — "
                       "発注経路がありません。起動を中止します[/red]")
        raise SystemExit(1)
    if runtime is None:
        _console.print("[red][FATAL] orchestrator の構築に失敗しました。"
                       "起動を中止します[/red]")
        raise SystemExit(1)
    if mode != "live":
        logger.warning(f"[ORCH] mode={mode} — 発注は行われません (検証運転)")


def run_startup_sequence(*, build, validate, initialize, start_api, start_scheduler):
    """build → validate → initialize → start → API → scheduler の順序を固定する
    seam (spec §1.5 / plan レビュー Medium-5 / High-1)。

    initialize は初回 news/tech 収集。orchestrator の planning/watch loop は
    start() 直後から動くため、初回収集を start() より前に置き、古い snapshot で
    判断・発注させない。どの段の失敗 (例外 / SystemExit) でも後段は実行されない。
    """
    runtime = build()
    validate(runtime)
    initialize()
    if runtime is not None:
        runtime.start()
    start_api()
    start_scheduler()
    return runtime
```

`main.py` の再構成:
1. 現行の API 起動 (:499-506)・scheduler 起動 (:543-544)・orchestrator 構築
   (:551-573) を `run_startup_sequence` 呼び出しに再編する:
   ```python
   from src.startup import ensure_orchestrator_or_exit, run_startup_sequence
   from src.orchestrator.bootstrap import build_orchestrator_runtime

   def _build():
       return build_orchestrator_runtime(
           config, store=store, price_store=price_store,
           analysis_store=analysis_store, price_provider=price_provider,
           cadence_resolver=_cadence_resolver,
       )   # 例外はそのまま落とす (fail-fast)

   def _validate(runtime):
       ensure_orchestrator_or_exit(
           runtime, enabled=config.orchestrator.enabled,
           mode=config.orchestrator.mode)

   orchestrator = run_startup_sequence(
       build=_build, validate=_validate,
       initialize=_initial_collection,    # 既存 Initial collection (:510-540) を関数化
       start_api=_start_api,              # 既存 start_api_server 呼び出しを関数化
       start_scheduler=_start_scheduler,  # 既存 scheduler_thread.start() を関数化
   )
   ```
   Initial collection (news/tech、:510-540) は `_initial_collection` として関数化し、
   **runtime.start() より前** に実行する (High-1: 古い snapshot での判断・発注防止)。
2. mode=shadow のとき Schedule 表示に
   `sched_table.add_row("Orchestrator", "[yellow]shadow — 発注なし[/yellow]")` を追加。
   live のときは `"live"` を表示。

- [ ] **Step 3: green 確認 + commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_main_failfast.py tests/test_orchestrator_bootstrap*.py tests/test_main_wiring.py -q 2>&1 | tail -3 && git add -A && git commit -m 'feat(main): fail-fast起動 (orchestrator必須/構築前倒し/live pair整合/shadow警告)'"
```

---

### Task 8: 取引サイクル系 完全削除 + reflection job 登録

> **順序意図 (plan レビュー Medium-4)**: fail-fast (Task 7) を先に入れてから
> 削除することで、削除後の中間コミットでも発注経路の消失が起動時に検出される。

**reflection job 登録 (このタスクの同一コミットで実施 — 旧経路削除と同時):**

`main.py`:
1. `_guards` に追加 (skip_predicate なし — 決済は休場中も残るため):
   ```python
   "reflection": JobGuard("reflection"),
   ```
2. ジョブ登録 (exit_check 登録の直後、毎時):
   ```python
   # 決済振り返り (毎時・LLM slot 1件ずつ・spec §3.6)
   from src.data.orchestrator_store import OrchestratorStore as _OrchStoreForReflect
   _reflect_store = _OrchStoreForReflect(config.prices_db_path)
   from src.cycles.reflection import run_reflection_cycle
   for t in technical_times:
       schedule.every().day.at(t, news_tz).do(
           _run_with_guard, _guards["reflection"],
           run_reflection_cycle, config, store, _reflect_store, slot=_llm_slot,
       )
   ```
3. Schedule 表示に行追加:
   ```python
   sched_table.add_row("Reflection", "every :00  (close reflection, 1-per-slot)")
   ```
4. `tests/test_main_wiring.py` に reflection ジョブ登録の検証を追記
   (schedule のジョブ一覧に reflection guard 経由の登録が 24 件あること)。

**Files (削除):**
- Delete: `src/cycles/trading.py`、`src/data/session_store.py`、
  `src/persistence/adaptive_params_store.py`、`src/analysis/performance_audit.py`、
  `src/analysis/audit_post_hoc.py`、`src/analysis/audit_report.py`、
  `src/signals/rag_adjustment.py`、`src/trading/atr_calculator.py`、
  `src/trading/entry_context_builder.py` (SLTPResult を import する唯一の同伴者。
  consumer は trading.py:54 のみ — orchestrator/context_builder.py:103 はコメント言及のみ)
- Modify: `src/cycles/__init__.py` (:14 trading import 削除)
- Modify: `src/trading_cycle.py` — **shim 全削除**し、参照元を直 import に更新:
  - `main.py:39` → `from src.cycles.exit_check import run_exit_check_cycle`
  - `src/views.py:72` → `from src.cycles._helpers import _summarize_pair`
- Modify: `src/cycles/_helpers.py` — `_get_ohlcv`/`_compute_atr_from_price_data`/
  `_fetch_and_compute_atr`/`_build_macro_context` 削除 (consumer 全滅)。
  `_get_price`/`_summarize_pair` 温存。`__all__` 整理。
- Modify: `src/rag/directional_writer.py` — `record_trade_entry` (:35-68)、
  `record_hold_review` (:200-232)、`_normalize_direction` (:20-29) 削除
- Modify: `src/data/analysis_store.py` — `_HoldDecisionRecord`/`HoldDecisionStore`
  (446-527) 削除
- Modify: `src/notifications/notifier.py` — `OrderOpenedEvent` (:20-32)、
  `SignalSkippedEvent` (:47-59)、`CycleSummaryEvent` (:92-99)、
  `classify_hold_reasons` (:229-262, accuracy_gate 含む)、`format_decision_line`、
  `format_health_line`、`_format_signal_block`、`_format_cycle_summary`、
  `notify_order_opened` (:354-373)、`notify_signal_skipped` (:437-458)、
  `notify_cycle_summary` (:460-462)、`_SOURCE_LABELS["trading"]` 削除。
  **温存**: `OrderClosedEvent`/`notify_order_closed` (exit_check 使用)、
  `PriceAlertEvent`/`notify_price_alert` (price_monitor 使用)。
- Modify: `src/api/routes/trading.py` (:48-116 `/run/trade` 削除、`/close/{pair}` 温存)、
  `src/api/_state.py` (:32 hold_store)、`src/api/server.py` (:68/:78/docstring)
- Modify: `src/views.py` (:23 SessionStore import、:215/:223)、
  `src/rag/ask_context_builder.py` (`build_trade_summary` :49-85、
  `_build_trade_summary` :313-327、session_store 引数) + `prompts/ask_user.j2` の
  trade_summary 変数
- Modify: `src/cli.py` (:23/:35-36/:38/:209-236/:409-416/:446-447、
  `run_commands` の hold_store 引数)
- Modify: `src/tui.py` (:279/:310-321)
- Modify: `src/api/routes/health.py` (:283/:337-340)
- Modify: `main.py` (:36 HoldDecisionStore、:39、:53-54 相当 guard 整理、
  :68-85 `_market_aware` 分岐、:215-216、:243 run_times、:323、:418-426 登録、
  :589、`_llm_slot` 系で trading 専用部)
- Config: `src/config/schema.py` (:145-153 rag_adjustment 9 キー、:155 atr_timeframe、
  :156-161 sl/tp_atr_mult 6 キー、:315 run_times、:373/:375/:377 notify 3 キー)、
  `config/settings.yaml.example` (:155-163/:165-171/:213-215/:343/:345/:347)
- Modify: `src/data/price_provider.py` (:195 run_times 加算削除)
- Tests 削除: `tests/test_trading_cycle_summary.py`、`tests/test_trading_cycle_halt.py`、
  `tests/test_cycle_summary.py`、`tests/test_rag_adjustment.py`、
  `tests/test_session_store.py`、`tests/test_audit_session_store.py`、
  `tests/test_adaptive_params_store.py`、`tests/test_audit_post_hoc.py`、
  `tests/test_audit_report.py`、`tests/test_audit_integration.py`、
  `tests/test_audit_real_config_integration.py`、`tests/test_audit_no_review.py`、
  `tests/fixtures/audit/`、`tests/test_reflector.py` (旧版)、
  `tests/test_atr_calculator.py`、`tests/test_atr_base_interval.py`、
  `tests/test_entry_context_builder.py` (存在すれば — atr/entry_context 退役分)
- Tests 修正: `tests/test_trading_cycle_helpers.py` (削除関数分を除去、
  `_summarize_pair`/`_get_price` 分は温存)、`tests/test_taskf_single_writer_guard.py`
  (trading cycle 依存部の扱いは内容確認の上判断 — orchestrator 側検証なら温存)、
  `tests/test_main_wiring.py`、`tests/test_config_loader.py`、
  `tests/test_config_example_sync.py`、`tests/test_notifier_close_labels.py` (要確認)

- [ ] **Step 1: 上記リストを順に削除・修正する**

注意点:
- `_llm_slot`/`_run_with_slot` 自体は news 収集が使うため温存。`_market_aware`
  パラメータ分岐 (main.py:75/78-79) のみ削除 (caller が trading のみ)。
- exit_check は独立 (trading.py から import なし — 実測済み) なので無傷。
- `Order` dataclass / `PositionManager` / `StateStore` / halt_state は温存
  (exit_check・orchestrator・reflection job が使用)。
- `TradeSignal` / `_calculate_position_size` / `combine_signals` は温存。

- [ ] **Step 2: 参照残りゼロ確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && grep -rn 'run_trading_cycle\|trading_cycle\|HoldDecisionStore\|AdaptiveParamsStore\|SessionStore\|rag_adjustment\|record_trade_entry\|record_hold_review\|performance_audit\|audit_post_hoc\|audit_report\|CycleSummaryEvent\|SignalSkippedEvent\|OrderOpenedEvent\|notify_cycle_summary\|notify_order_opened\|notify_signal_skipped\|run_times\|atr_timeframe\|sl_atr_mult\|tp_atr_mult\|calculate_sl_tp\|SLTPResult\|entry_context_builder\|build_entry_context\|_compute_atr_from_price_data\|_fetch_and_compute_atr\|_build_macro_context' src/ main.py config/settings.yaml.example --include='*.py' --include='*.example' | grep -v Binary"
```

Expected: ヒット 0 件 (`src/trading_cycle.py` 自体も削除済み)。

- [ ] **Step 3: 影響 per-file テスト green 確認**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_exit_check.py tests/test_trading_cycle_helpers.py tests/test_signal_combiner.py tests/test_main_wiring.py tests/test_api_server.py tests/test_notifier_close_labels.py tests/test_config_loader.py tests/test_config_example_sync.py -q 2>&1 | tail -3"
```

(ファイル名は実在に合わせ調整。exit_check テストの実名を確認して実行。)

- [ ] **Step 4: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && git add -A && git commit -m 'feat!: 取引サイクル完全削除 + reflection job登録 (旧新経路の重複なし切替)'"
```

---

### Task 9: migration スクリプト

**Files:**
- Create: `scripts/migrate_cycle_retirement.py`
- Test: `tests/test_migrate_cycle_retirement.py` (新規)

spec: §4。雛形は `scripts/migrate_directional_rag.py` (sys.path + load_config パターン)。

- [ ] **Step 1: failing test を書く**

`tests/test_migrate_cycle_retirement.py` — スクリプトの中核関数を import してテスト:

```python
"""migration の冪等性テスト (spec §4)。"""
import sqlite3

from scripts.migrate_cycle_retirement import delete_adaptive_params, drop_retired_tables


def _make_db(tmp_path):
    db = tmp_path / "prices.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE hold_decisions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE trading_sessions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE technical_snapshots (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db


def _tables(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_drops_only_retired_tables(tmp_path):
    db = _make_db(tmp_path)
    dropped = drop_retired_tables(db)
    assert dropped == ["forecasts", "hold_decisions", "trading_sessions"]
    assert _tables(db) == {"technical_snapshots"}


def test_drop_idempotent(tmp_path):
    db = _make_db(tmp_path)
    drop_retired_tables(db)
    assert drop_retired_tables(db) == []


def test_delete_adaptive_params(tmp_path):
    # 実ファイル名は adaptive_params.yaml (adaptive_params_store.py:11 _FILENAME)
    f = tmp_path / "adaptive_params.yaml"
    f.write_text("{}")
    assert delete_adaptive_params(tmp_path) is True
    assert not f.exists()
    assert delete_adaptive_params(tmp_path) is False   # 冪等
```

- [ ] **Step 2: 実装**

`scripts/migrate_cycle_retirement.py`:

```python
"""forecast/取引サイクル退役 migration (spec §4)。

Usage:
    uv run python scripts/migrate_cycle_retirement.py

前提: システム停止中に実行し、実行前に以下をバックアップ済みであること。
  - prices.db (DB)
  - data/ 配下の RAG 永続化先 (ChromaDB)
  - state_dir (adaptive_params.yaml 含む)

冪等: 再実行しても安全 (DROP IF EXISTS / where 削除 / 存在チェック)。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_RETIRED_TABLES = ["forecasts", "hold_decisions", "trading_sessions"]
_ADAPTIVE_FILENAME = "adaptive_params.yaml"   # adaptive_params_store.py:11 の実値


def drop_retired_tables(db_path) -> list[str]:
    conn = sqlite3.connect(db_path)
    dropped = []
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _RETIRED_TABLES:
            if table in existing:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                dropped.append(table)
        conn.commit()
    finally:
        conn.close()
    return dropped


def delete_adaptive_params(state_dir) -> bool:
    f = Path(state_dir) / _ADAPTIVE_FILENAME
    if f.exists():
        f.unlink()
        return True
    return False


def main() -> None:
    from src.config import load_config
    from src.rag.vector_store import VectorStore   # migrate_directional_rag.py と同パターン

    config = load_config()
    print("== cycle retirement migration ==")
    dropped = drop_retired_tables(config.prices_db_path)
    print(f"dropped tables: {dropped or '(none — already migrated)'}")
    if delete_adaptive_params(config.state_dir):
        print("deleted adaptive_params.yaml")
    else:
        print("adaptive_params.yaml not present")
    store = VectorStore(config.rag_db_path)   # main.py:212 と同構築
    counts = store.directional.delete_retired_cards()
    print(f"deleted RAG cards: {counts}")
    print("done.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: green 確認 + commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_migrate_cycle_retirement.py tests/test_directional_store_filters.py -q 2>&1 | tail -3 && git add scripts/migrate_cycle_retirement.py tests/test_migrate_cycle_retirement.py && git commit -m 'feat(migration): 退役テーブルdrop + adaptive JSON削除 + RAG退役カード削除 (冪等)'"
```

---

### Task 10: 全体回帰 — full suite + 実 config 掃除チェックリスト

- [ ] **Step 1: full suite 実行 (合格基準)**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest -q 2>&1 | tail -10"
```

Expected: **失敗は既知のみ** (`tests/test_insights.py` ChromaDB 系 2 件、CLAUDE.md 基準)。
それ以外の失敗はすべて解消してから次へ。

- [ ] **Step 2: 実 config 掃除の TODO を deploy ノートに残す**

`docs/superpowers/notes/2026-07-18-cycle-retirement-deploy.md` を作成 (§8 の順序 +
§5 の実 config 削除キー一覧 + §4 のバックアップ手順を転記)。
デプロイは本 plan のスコープ外 (ユーザー実施)。

- [ ] **Step 3: ノートはローカル保存のみ**

deploy note は commit しない (docs/ は gitignore 運用 — plan レビュー Low-4)。
ユーザーが必要と判断すれば手動で `git add -f` する。

---

### Task 11: discord_bot 追従 (別リポジトリ)

**Files (discord_bot リポジトリ `~/project/discord_bot`):**
- Modify: `cogs/finance/client.py` (:79-83 `forecast`、:94-95 `run_trade` 削除)
- Modify: `cogs/finance/finance_cog.py`
  (:65-84 `finance_forecast` schema、:109-116 `finance_run_trade` schema、
  :243/:246 `TOOL_METHODS`、:260 `ADMIN_TOOLS`、:513-557 `_forecast_embeds`、
  :719-756 `_run_trade_embed`、:1256-1266 `forecast` コマンド、
  :1290-1302 `run` コマンド)

- [ ] **Step 1: discord_bot に作業ブランチを切る**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/discord_bot && git checkout master && git checkout -b feat/finance-cycle-retirement"
```

- [ ] **Step 2: 上記削除を実施**

- [ ] **Step 3: 参照残りゼロ + 全テスト**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/discord_bot && grep -rn 'forecast\|run_trade\|run/trade' cogs/finance/ --include='*.py' | grep -v gate" 
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/discord_bot && uv run pytest -q 2>&1 | tail -3"
```

Expected: grep は gate 系以外ヒット 0 件、pytest 全 PASS
(既存テストは gate 系のみで forecast/run_trade に依存しない — 調査済み)。

- [ ] **Step 4: commit**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/discord_bot && git add -A && git commit -m 'feat: finance forecast/run_trade 導線削除 (cycle retirement 追従)'"
```

---

## 完了条件

1. finance full suite: 既知失敗 (`test_insights.py` 2 件) のみ
2. discord_bot full suite: 全 PASS
3. grep 検証 (Task 6/8 の grep 検証 Step) ヒット 0 件
4. spec §1.5 fail-fast がテストで担保されている
5. デプロイノートが存在する (Task 10)
