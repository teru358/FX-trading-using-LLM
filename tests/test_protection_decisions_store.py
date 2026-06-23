from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


def test_record_and_compare_protection_decisions(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()

    # 同一 order_id・近接 ts で両 source が同じ判定 → 一致
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o1", source="price_monitor",
        action="raise_sl", stage="breakeven", target_sl=150.0,
        mfe_r=0.6, giveback_r=0.1,
    )
    orch.record_protection_decision(
        ts=now + timedelta(seconds=1), pair="USDJPY=X", order_id="o1",
        source="tick_worker", action="raise_sl", stage="breakeven",
        target_sl=150.0, mfe_r=0.6, giveback_r=0.1,
    )

    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    r = rows[0]
    assert r["order_id"] == "o1"
    assert r["action_match"] is True
    assert r["target_sl_match"] is True


def test_compare_detects_mismatch(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o2", source="price_monitor",
        action="none", stage=None, target_sl=None, mfe_r=0.1, giveback_r=0.0,
    )
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o2", source="tick_worker",
        action="raise_sl", stage="half", target_sl=149.5, mfe_r=0.4, giveback_r=0.0,
    )
    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    assert rows[0]["action_match"] is False


def test_compare_skips_far_apart_records(tmp_path: Path):
    """ts が max_delta_seconds を超えて離れたペアは比較対象外 (review M-e)。

    tick_worker は 2s 毎、price_monitor は数分毎なので、別局面同士を突き合わせて
    false match/mismatch を出さないよう近接 ts のみペアリングする。
    """
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o3", source="price_monitor",
        action="raise_sl", stage="half", target_sl=149.5, mfe_r=0.4, giveback_r=0.0,
    )
    # tick_worker は 10 分後 (別局面) → ペアリングしない
    orch.record_protection_decision(
        ts=now + timedelta(minutes=10), pair="USDJPY=X", order_id="o3",
        source="tick_worker", action="close", stage="giveback", target_sl=None,
        mfe_r=1.0, giveback_r=0.5,
    )
    rows = orch.compare_protection_decisions(
        since=now - timedelta(hours=1), max_delta_seconds=60
    )
    assert rows == []  # 60s を超えて離れているのでペア無し


def test_compare_pairs_nearest_within_delta(tmp_path: Path):
    """同 order_id で複数行があるとき、最も近い ts 同士をペアにする (review M-e)。"""
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    # price_monitor: now
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o4", source="price_monitor",
        action="raise_sl", stage="half", target_sl=149.5, mfe_r=0.4, giveback_r=0.0,
    )
    # tick_worker: now+1s (近接, ペア候補) と now+30min (遠い)
    orch.record_protection_decision(
        ts=now + timedelta(seconds=1), pair="USDJPY=X", order_id="o4",
        source="tick_worker", action="raise_sl", stage="half", target_sl=149.5,
        mfe_r=0.4, giveback_r=0.0,
    )
    orch.record_protection_decision(
        ts=now + timedelta(minutes=30), pair="USDJPY=X", order_id="o4",
        source="tick_worker", action="close", stage="giveback", target_sl=None,
        mfe_r=1.0, giveback_r=0.6,
    )
    rows = orch.compare_protection_decisions(
        since=now - timedelta(hours=1), max_delta_seconds=60
    )
    assert len(rows) == 1
    assert rows[0]["action_match"] is True  # 近接ペア (raise_sl vs raise_sl)


def test_target_sl_match_tolerates_float_noise(tmp_path):
    from datetime import timedelta
    from src.data.orchestrator_store import OrchestratorStore
    from src.utils.clock import db_now
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="oF", source="price_monitor",
        action="raise_sl", stage="half", target_sl=150.0, mfe_r=0.4, giveback_r=0.0,
    )
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="oF", source="tick_worker",
        action="raise_sl", stage="half", target_sl=150.0 + 1e-9, mfe_r=0.4, giveback_r=0.0,
    )
    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    assert rows[0]["target_sl_match"] is True  # 1e-9 差は許容


def test_target_sl_match_both_none_is_match(tmp_path):
    from datetime import timedelta
    from src.data.orchestrator_store import OrchestratorStore
    from src.utils.clock import db_now
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="oN", source="price_monitor",
        action="none", stage=None, target_sl=None, mfe_r=0.1, giveback_r=0.0,
    )
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="oN", source="tick_worker",
        action="none", stage=None, target_sl=None, mfe_r=0.1, giveback_r=0.0,
    )
    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert rows[0]["target_sl_match"] is True
