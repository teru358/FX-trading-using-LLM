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
