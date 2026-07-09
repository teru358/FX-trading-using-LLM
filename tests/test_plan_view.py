"""plan_view.plan_to_row の整形テスト。

CLI 表示と将来の承認ゲート F-5 API が共用する整形層。_TradePlan を表示/API 用の
dict に変換する。sl/tp 欠損や entry 条件無しでも落ちず None/空へフォールバックする。
(spec: docs/superpowers/specs/2026-07-07-cli-plans-command-design.md §3.2)
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from src.orchestrator.plan_view import plan_to_row


def _plan(**over):
    """_TradePlan 相当の属性を持つダミー (plan_to_row は属性アクセスのみ)。"""
    base = dict(
        plan_id=7, pair="USDJPY=X", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.8, "tp": 151.5},
        expires_at=datetime(2026, 6, 27, 12, 0, 0),
        created_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_plan_to_row_full():
    row = plan_to_row(_plan())
    assert row["plan_id"] == 7
    assert row["pair"] == "USDJPY=X"
    assert row["direction"] == "long"
    assert row["entry_summary"] == "price_at_or_below 150.3"
    assert row["sl"] == 149.8
    assert row["tp"] == 151.5
    assert row["expires_at"] == datetime(2026, 6, 27, 12, 0, 0)
    assert row["created_at"] == datetime(2026, 6, 20, 12, 0, 0)


def test_plan_to_row_missing_action_json():
    row = plan_to_row(_plan(action_json=None))
    assert row["sl"] is None
    assert row["tp"] is None


def test_plan_to_row_no_entry_conditions():
    row = plan_to_row(_plan(entry_conditions_json=[]))
    assert row["entry_summary"] == ""


def test_plan_to_row_malformed_entry_conditions_does_not_raise():
    """要素が dict でない壊れた entry_conditions でも落ちず空文字に倒す。"""
    row = plan_to_row(_plan(entry_conditions_json=["broken", None]))
    assert row["entry_summary"] == ""


def test_plan_to_row_malformed_action_json_type():
    """action_json が dict でない場合も sl/tp は None に倒す。"""
    row = plan_to_row(_plan(action_json="not-a-dict"))
    assert row["sl"] is None
    assert row["tp"] is None
