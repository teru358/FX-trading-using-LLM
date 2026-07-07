"""_PollingAccessFilter の drop/keep 判定テスト。

uvicorn.access のレコードは
`record.args == (client_addr, method, full_path, http_version, status_code)`。
GET /quote/* の 2xx (毎秒 polling の成功) だけ drop し、それ以外は keep する。
(spec: docs/superpowers/specs/2026-07-07-quote-tick-log-suppression-design.md)
"""
from __future__ import annotations

import logging

import server


def _access_record(args) -> logging.LogRecord:
    """uvicorn.access が出す形の LogRecord を組み立てる。"""
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=0,
        msg='%s - "%s %s HTTP/%s" %d', args=args, exc_info=None,
    )


def _filter() -> logging.Filter:
    return server._PollingAccessFilter()


def test_quote_2xx_dropped():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/USDJPY", "1.1", 200))
    assert _filter().filter(rec) is False


def test_quote_204_dropped():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/EURUSD", "1.1", 204))
    assert _filter().filter(rec) is False


def test_quote_5xx_kept():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/USDJPY", "1.1", 500))
    assert _filter().filter(rec) is True


def test_health_2xx_kept():
    """/health は低頻度 (preflight / halt resume / app proxy) のため抑制しない。"""
    rec = _access_record(("192.168.1.10:50000", "GET", "/health", "1.1", 200))
    assert _filter().filter(rec) is True


def test_post_order_kept():
    rec = _access_record(("192.168.1.10:50000", "POST", "/order", "1.1", 200))
    assert _filter().filter(rec) is True


def test_ohlcv_2xx_kept():
    rec = _access_record(("192.168.1.10:50000", "GET", "/ohlcv/USDJPY", "1.1", 200))
    assert _filter().filter(rec) is True


def test_malformed_args_none_kept():
    """args が想定形状でない場合は fail-open (落とすより出す)。"""
    assert _filter().filter(_access_record(None)) is True


def test_malformed_args_short_tuple_kept():
    assert _filter().filter(_access_record(("client", "GET", "/quote/USDJPY"))) is True


def test_malformed_path_not_str_kept():
    """5-tuple だが path が str でない場合も fail-open で keep。"""
    assert _filter().filter(_access_record(("c", "GET", None, "1.1", 200))) is True


def test_malformed_status_not_int_kept():
    """5-tuple だが status が int でない場合も fail-open で keep。"""
    assert _filter().filter(
        _access_record(("c", "GET", "/quote/USDJPY", "1.1", "200"))
    ) is True


def test_log_config_wires_polling_filter_into_access_handler():
    """main() 内の配線漏れを検出する: access ハンドラに polling filter が入っている。"""
    cfg = server._build_log_config()
    assert "polling_access" in cfg["handlers"]["access"]["filters"]
    assert cfg["filters"]["polling_access"]["()"] is server._PollingAccessFilter


def test_log_config_keeps_existing_uvicorn_name_fix():
    """既存の uvicorn.error 表示名書き換えが関数化後も残っている (回帰防止)。"""
    cfg = server._build_log_config()
    assert "uvicorn_name_fix" in cfg["handlers"]["default"]["filters"]
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["access"]
