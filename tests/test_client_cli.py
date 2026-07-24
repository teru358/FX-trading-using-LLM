# tests/test_client_cli.py
"""repo 直下 client.py (対話型クライアント) の回帰テスト。

Task 6 (forecast 系削除) の外部レビューで client.py が grep 射程外だったため
`run forecast` コマンドが残存した。その再発防止として、退役済みコマンド
(forecast / trade) が CLI に存在しないことを検証する。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import client  # noqa: E402  (repo 直下の client.py)


def test_forecast_absent_from_client_source():
    """client.py ソース全体に forecast への参照が残っていない。"""
    source = Path(client.__file__).read_text(encoding="utf-8")
    assert "forecast" not in source.lower()


def test_cmd_forecast_function_removed():
    assert not hasattr(client, "_cmd_forecast")


def test_help_has_no_forecast_but_keeps_other_run_commands():
    assert "forecast" not in client._HELP.lower()
    # 温存コマンドが誤って消えていないこと (sanity)
    assert "run news" in client._HELP
    assert "run tech" in client._HELP
    assert "run analyze" in client._HELP


def test_run_forecast_falls_to_unknown_subcommand():
    """`run forecast` は未知サブコマンド扱い (HTTP 呼び出しに到達しない)。"""
    with client._console.capture() as cap:
        result = client._dispatch("run forecast")
    assert result is True  # 終了しない
    assert "不明" in cap.get()


def test_forecast_top_level_is_unknown_command():
    with client._console.capture() as cap:
        result = client._dispatch("forecast")
    assert result is True
    assert "不明なコマンド" in cap.get()


# ── run trade 退役 (Task 8) ────────────────────────────────────────────


def test_cmd_run_trade_function_removed():
    assert not hasattr(client, "_cmd_run_trade")


def test_run_trade_absent_from_client_source():
    """client.py に /run/trade 呼び出しと run trade の導線が残っていない。"""
    source = Path(client.__file__).read_text(encoding="utf-8")
    assert "/run/trade" not in source
    assert "_cmd_run_trade" not in source
    assert "run trade" not in source


def test_help_has_no_run_trade():
    assert "run trade" not in client._HELP


def test_run_trade_falls_to_unknown_subcommand():
    """`run trade` は未知サブコマンド扱い (HTTP 呼び出しに到達しない)。"""
    with client._console.capture() as cap:
        result = client._dispatch("run trade")
    assert result is True
    assert "不明" in cap.get()


# ── plans コマンド (REST 経由 GET /orchestrator/plans) ─────────────────

def _plan_row(**kw):
    base = {
        "plan_id": 1,
        "pair": "USDJPY=X",
        "direction": "long",
        "entry_summary": "price<=150.0",
        "sl": 149.5,
        "tp": 151.0,
        "rr": 2.0,
        "expires_at": "2026-07-24T18:00:00",
        "created_at": "2026-07-24T12:00:00",
    }
    base.update(kw)
    return base


def test_help_has_plans():
    assert "plans" in client._HELP


def test_dispatch_routes_plans(monkeypatch):
    called = {}
    monkeypatch.setattr(client, "_cmd_plans", lambda: called.setdefault("hit", True))
    assert client._dispatch("plans") is True
    assert called.get("hit")


def test_cmd_plans_queries_both_statuses(monkeypatch):
    """pending_approval と active の両方を GET する。"""
    seen = []

    def fake_get(path, params=None):
        seen.append((params or {}).get("status"))
        return {"plans": []}

    monkeypatch.setattr(client, "_get", fake_get)
    client._cmd_plans()
    assert "pending_approval" in seen
    assert "active" in seen


def test_cmd_plans_renders_rows(monkeypatch):
    def fake_get(path, params=None):
        status = (params or {}).get("status")
        if status == "pending_approval":
            return {"plans": [_plan_row(plan_id=7)]}
        return {"plans": [_plan_row(plan_id=3, direction="short")]}

    monkeypatch.setattr(client, "_get", fake_get)
    # Rich は console 幅 (既定 80) で列を切り詰めるため、幅を広げて全列を出す
    from rich.console import Console
    monkeypatch.setattr(client, "_console", Console(width=200))
    with client._console.capture() as cap:
        client._cmd_plans()
    out = cap.get()
    assert "USDJPY=X" in out
    assert "149.500" in out          # SL (承認待ちグループ)
    assert "承認待ち" in out and "発注監視中" in out


def test_cmd_plans_broken_row_does_not_crash(monkeypatch):
    """sl/tp None (壊れた action_json) でも落ちない。"""
    def fake_get(path, params=None):
        return {"plans": [_plan_row(sl=None, tp=None, rr=None, entry_summary="")]}

    monkeypatch.setattr(client, "_get", fake_get)
    client._cmd_plans()  # 例外なく完了すれば OK


def test_cmd_plans_api_failure_returns_quietly(monkeypatch):
    """_get が None (通信断) を返しても落ちない。"""
    monkeypatch.setattr(client, "_get", lambda path, params=None: None)
    client._cmd_plans()
