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
