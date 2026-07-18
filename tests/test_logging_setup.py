"""setup_logging() のサードパーティロガー抑制の検証。

quote-stream producer が毎秒 /quote を polling するため、httpx の
`HTTP Request: GET ... 200 OK` INFO 行と httpcore の DEBUG 行がターミナル・
main log を汚染する。setup_logging() がこれらを WARNING に降格することを確認する。
(spec: docs/superpowers/specs/2026-07-07-quote-tick-log-suppression-design.md)
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.logging_setup import _ACTIVITY_PREFIXES, _PREFIX_STYLES, setup_logging


@pytest.fixture
def restore_logging():
    """setup_logging() が root logger をグローバルに書き換えるため、テスト後に復元する。

    uvicorn.access は setup_logging() が呼び出しごとに _ApiAccessPrefixFilter を
    addFilter するため、filters も復元しないと実行順によって蓄積する。
    """
    root = logging.getLogger()
    uvicorn_access = logging.getLogger("uvicorn.access")
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_access_filters = uvicorn_access.filters[:]
    saved_third_party = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore")
    }
    yield
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()  # tmp_path 内のログファイルハンドルを解放
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    uvicorn_access.filters[:] = saved_access_filters
    for name, lvl in saved_third_party.items():
        logging.getLogger(name).setLevel(lvl)


def _logging_cfg():
    """setup_logging() が参照する属性だけ持つ最小 config。"""
    return SimpleNamespace(
        file="logs/main.log",
        activity_log_file="logs/activity.log",
        level="INFO",
        rotate_timing="10MB",
        backup_count=1,
    )


def test_httpx_and_httpcore_demoted_to_warning(restore_logging, tmp_path):
    setup_logging(_logging_cfg(), tmp_path)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_orch_prefix_registered_for_activity():
    """[ORCH] は activity.log 対象 (goes_to_activity_log=True) に登録されている。"""
    assert "[ORCH]" in _ACTIVITY_PREFIXES


def test_orch_prefix_has_style():
    """[ORCH] は着色スタイルを持つ。"""
    assert "[ORCH]" in _PREFIX_STYLES
