"""cli plans 表示ヘルパ (_fmt_dt / _fmt_num) と _cmd_plans のテスト。

Rich テーブル本体は薄いが、None/非数値フォールバックを持つ整形ヘルパは純関数として
単体検証し、_cmd_plans は fake store + StringIO console で見出し/(なし)/DB例外を検証する。
(spec: docs/superpowers/specs/2026-07-07-cli-plans-command-design.md §3.3)
"""
from __future__ import annotations

from datetime import datetime
from io import StringIO
from types import SimpleNamespace

import src.cli as cli
from src.cli import _cmd_plans, _fmt_dt, _fmt_num


def test_fmt_dt_formats_month_day_hour_min():
    assert _fmt_dt(datetime(2026, 7, 8, 9, 5, 0)) == "07-08 09:05"


def test_fmt_dt_none_returns_dash():
    assert _fmt_dt(None) == "-"


def test_fmt_num_formats_three_decimals():
    assert _fmt_num(149.8) == "149.800"


def test_fmt_num_none_returns_dash():
    assert _fmt_num(None) == "-"


def test_fmt_num_non_numeric_returns_dash():
    """float 変換できない値 (壊れた action_json) でも落ちず「-」。"""
    assert _fmt_num("bad") == "-"


class _FakeStore:
    """OrchestratorStore の get_plans_by_status だけ差し替える fake。"""

    def __init__(self, mapping):
        self._mapping = mapping  # status tuple の先頭 -> list[_TradePlan 相当]

    def get_plans_by_status(self, statuses, pair=None):
        return self._mapping.get(statuses[0], [])


def _fake_plan(pair="USDJPY=X", direction="long"):
    return SimpleNamespace(
        plan_id=1, pair=pair, direction=direction,
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.8, "tp": 151.5},
        expires_at=datetime(2026, 7, 8, 9, 0, 0),
        created_at=datetime(2026, 7, 7, 22, 0, 0),
    )


def _capture_console(monkeypatch):
    """_console を StringIO 出力の Console に差し替え、出力バッファを返す。"""
    from rich.console import Console
    buf = StringIO()
    monkeypatch.setattr(cli, "_console", Console(file=buf, force_terminal=False, width=200))
    return buf


def test_cmd_plans_shows_pending_and_active(monkeypatch):
    monkeypatch.setattr(
        cli, "OrchestratorStore",
        lambda _p: _FakeStore({"pending_approval": [_fake_plan()], "active": [_fake_plan()]}),
    )
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert "pending_approval" in out
    assert "active" in out
    assert "USDJPY=X" in out


def test_cmd_plans_empty_shows_nashi(monkeypatch):
    monkeypatch.setattr(
        cli, "OrchestratorStore",
        lambda _p: _FakeStore({}),  # 両方 0 件
    )
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert out.count("なし") == 2  # pending / active 両方 (なし)


def test_cmd_plans_db_error_is_reported(monkeypatch):
    def _boom(_p):
        raise RuntimeError("db locked")
    monkeypatch.setattr(cli, "OrchestratorStore", _boom)
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert "DB 読み取り失敗" in out
    assert "RuntimeError" in out
