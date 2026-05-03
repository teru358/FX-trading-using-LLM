"""MT5 ブリッジ heartbeat ジョブのユニットテスト。

httpx をモックして、成功/失敗/未設定/タイムアウトの各分岐で JSONL に
正しく記録されること、デーモン停止しないことを検証する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from src.config.schema import Mt5Config, ProvidersConfig, ScheduleConfig
from src.jobs.mt5_heartbeat import _append_record, run_mt5_heartbeat


def _make_cfg(*, live_broker: str | None = "mt5", **overrides) -> object:
    """テスト用の最小 AppConfig 風スタブ。

    live_broker=None (または "mt5" 以外) を指定すると providers.mt5 が
    存在しても heartbeat は noop になる挙動を再現できる。
    """
    @dataclass
    class _C:
        live_broker: str | None
        providers: ProvidersConfig
        schedule: ScheduleConfig = field(
            default_factory=lambda: ScheduleConfig(timezone="Asia/Tokyo")
        )

    base = dict(
        bridge_url="http://example.local:8812",
        heartbeat_interval_minutes=60,
        request_timeout_seconds=2.0,
        log_path="data/state/mt5_heartbeat.jsonl",
    )
    base.update(overrides)
    return _C(
        live_broker=live_broker,
        providers=ProvidersConfig(mt5=Mt5Config(**base)),
    )


# ── _append_record ────────────────────────────────────────────────


def test_append_record_creates_parent_and_writes_jsonl(tmp_path: Path):
    log = tmp_path / "nested" / "dir" / "hb.jsonl"
    _append_record(log, {"a": 1})
    _append_record(log, {"b": 2})

    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


# ── run_mt5_heartbeat (entry point) ──────────────────────────────


def test_disabled_is_noop(tmp_path: Path, monkeypatch):
    """live_broker != "mt5" のとき heartbeat は noop。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    cfg = _make_cfg(live_broker=None)
    run_mt5_heartbeat(cfg)
    # JSONL は作成されない
    assert not (tmp_path / "data/state/mt5_heartbeat.jsonl").exists()


def test_empty_url_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    cfg = _make_cfg(bridge_url="")
    run_mt5_heartbeat(cfg)
    assert not (tmp_path / "data/state/mt5_heartbeat.jsonl").exists()


def test_success_records_ok(tmp_path: Path, monkeypatch):
    """200 OK を返すようにモックし、ok=True で 1 行追記される。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    class _Resp:
        is_success = True
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())

    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    log = tmp_path / "data/state/mt5_heartbeat.jsonl"
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["ok"] is True
    assert rec["http_status"] == 200
    assert rec["error"] is None
    assert rec["url"].endswith("/health")
    assert isinstance(rec["latency_ms"], (int, float))


def test_connection_error_records_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    def _raise(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    log = tmp_path / "data/state/mt5_heartbeat.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["ok"] is False
    assert rec["http_status"] is None
    assert "ConnectError" in rec["error"]


def test_timeout_records_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    def _timeout(*a, **kw):
        raise httpx.TimeoutException("read timeout")

    monkeypatch.setattr(httpx, "get", _timeout)
    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    log = tmp_path / "data/state/mt5_heartbeat.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["ok"] is False
    assert "TimeoutException" in rec["error"]


def test_5xx_response_records_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    class _Resp:
        is_success = False
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    log = tmp_path / "data/state/mt5_heartbeat.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["ok"] is False
    assert rec["http_status"] == 503
    assert rec["error"] is None  # HTTP-level success だが 5xx → ok=False、エラー文字列なし


def test_unexpected_exception_does_not_crash_daemon(tmp_path: Path, monkeypatch, caplog):
    """予想外の例外でもデーモンは止まらない (logger.error のみ)。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    # _append_record 自体が壊れる状況を再現
    monkeypatch.setattr(
        "src.jobs.mt5_heartbeat._append_record",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    class _Resp:
        is_success = True
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    cfg = _make_cfg()
    with caplog.at_level("ERROR"):
        run_mt5_heartbeat(cfg)
    assert any("MT5_HB" in r.message for r in caplog.records)
