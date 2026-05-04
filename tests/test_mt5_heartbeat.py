"""MT5 ブリッジ heartbeat ジョブのユニットテスト。

httpx をモックして、成功/失敗/未設定/タイムアウトの各分岐で JSONL に
正しく記録されること、デーモン停止しないこと、/health 連続 N 回失敗で
auto soft halt が発動することを検証する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from src.config.schema import Mt5Config, NotifierConfig, ProvidersConfig, ScheduleConfig
from src.jobs.mt5_heartbeat import (
    _append_record,
    _reset_state_for_test,
    _state,
    run_mt5_heartbeat,
)


def _make_cfg(
    *, live_broker: str | None = "mt5",
    notifier_enabled: bool = False,
    **overrides,
) -> object:
    """テスト用の最小 AppConfig 風スタブ。

    live_broker=None (または "mt5" 以外) を指定すると providers.mt5 が
    存在しても heartbeat は noop になる挙動を再現できる。
    """
    @dataclass
    class _C:
        live_broker: str | None
        providers: ProvidersConfig
        notifier: NotifierConfig
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
        notifier=NotifierConfig(enabled=notifier_enabled),
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    """各テストの前後でモジュール状態をクリア (テスト間干渉を防ぐ)。"""
    _reset_state_for_test()
    yield
    _reset_state_for_test()


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


# ── auto soft halt 判定 (連続失敗カウントベース) ────────────────


def _stub_failing_get(monkeypatch):
    def _raise(*a, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "get", _raise)


def _capture_post(monkeypatch) -> list[dict]:
    """httpx.post 呼出を記録するスタブ。"""
    posts: list[dict] = []

    class _Resp:
        is_success = True
        status_code = 200

    def _post(url, *a, **kw):
        posts.append({"url": url, "json": kw.get("json"), "headers": kw.get("headers")})
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)
    return posts


def test_failures_below_threshold_do_not_halt(tmp_path: Path, monkeypatch):
    """連続失敗回数が閾値未満なら halt しない。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    _stub_failing_get(monkeypatch)
    posts = _capture_post(monkeypatch)

    cfg = _make_cfg(consecutive_unreachable_threshold=3)
    # 2 回失敗 (閾値=3 未満)
    run_mt5_heartbeat(cfg)
    run_mt5_heartbeat(cfg)

    assert _state["consecutive_failures"] == 2
    assert posts == []
    assert _state["auto_halt_triggered"] is False


def test_third_consecutive_failure_triggers_halt(tmp_path: Path, monkeypatch):
    """3 連続失敗で /admin/halt を呼ぶ + 以降は再発動しない。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    _stub_failing_get(monkeypatch)
    posts = _capture_post(monkeypatch)

    cfg = _make_cfg(
        consecutive_unreachable_threshold=3,
        api_key="test-key",
    )
    run_mt5_heartbeat(cfg)
    run_mt5_heartbeat(cfg)
    assert posts == []
    run_mt5_heartbeat(cfg)  # 3 回目で発動

    assert len(posts) == 1
    assert posts[0]["url"] == "http://example.local:8812/admin/halt"
    assert posts[0]["json"]["mode"] == "soft"
    assert "3 consecutive" in posts[0]["json"]["reason"]
    assert posts[0]["headers"] == {"X-Bridge-Api-Key": "test-key"}
    assert _state["auto_halt_triggered"] is True

    # 4 回目以降の失敗でも再発動しない
    run_mt5_heartbeat(cfg)
    assert len(posts) == 1


def test_success_resets_failure_counter(tmp_path: Path, monkeypatch):
    """成功プローブで連続失敗カウンタと auto_halt_triggered がリセットされる。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    class _Resp:
        is_success = True
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    posts = _capture_post(monkeypatch)

    _state["consecutive_failures"] = 5
    _state["auto_halt_triggered"] = True

    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    assert _state["consecutive_failures"] == 0
    assert _state["auto_halt_triggered"] is False
    assert posts == []  # 成功時は halt API 呼ばない


def test_intermittent_success_resets_streak(tmp_path: Path, monkeypatch):
    """失敗 → 成功 → 失敗 のパターンで連続カウントがリセットされる。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    posts = _capture_post(monkeypatch)

    responses = iter([False, False, True, False, False])

    class _RespOk:
        is_success = True
        status_code = 200

    def _get(*a, **kw):
        ok = next(responses)
        if not ok:
            raise httpx.ConnectError("nope")
        return _RespOk()

    monkeypatch.setattr(httpx, "get", _get)

    cfg = _make_cfg(consecutive_unreachable_threshold=3)
    run_mt5_heartbeat(cfg)  # fail 1
    run_mt5_heartbeat(cfg)  # fail 2
    assert _state["consecutive_failures"] == 2
    run_mt5_heartbeat(cfg)  # success → reset
    assert _state["consecutive_failures"] == 0
    run_mt5_heartbeat(cfg)  # fail 1 (リセット後)
    run_mt5_heartbeat(cfg)  # fail 2

    assert posts == []  # 連続 3 回には届かない
    assert _state["consecutive_failures"] == 2


def test_halt_api_failure_does_not_crash(tmp_path: Path, monkeypatch, caplog):
    """halt API 自体が失敗しても heartbeat ジョブは死なない。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    _stub_failing_get(monkeypatch)

    def _post_fail(*a, **kw):
        raise httpx.ConnectError("halt unreachable")
    monkeypatch.setattr(httpx, "post", _post_fail)

    _state["consecutive_failures"] = 2  # 次の失敗で 3 連続到達

    cfg = _make_cfg(consecutive_unreachable_threshold=3)
    with caplog.at_level("ERROR"):
        run_mt5_heartbeat(cfg)

    assert any(
        "auto halt API call failed" in r.message
        for r in caplog.records
    )


def test_notifier_invoked_when_enabled(tmp_path: Path, monkeypatch):
    """notifier.enabled=True で halt 発動時に Discord 通知 (send_embed) が呼ばれる。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    _stub_failing_get(monkeypatch)
    _capture_post(monkeypatch)

    send_calls: list[dict] = []

    class _StubNotifier:
        async def send_embed(self, **kwargs):
            send_calls.append(kwargs)

        async def send(self, message: str):
            pass

    monkeypatch.setattr(
        "src.notifications.notifier.create_notifier",
        lambda enabled: _StubNotifier(),
    )

    _state["consecutive_failures"] = 2  # 次の失敗で 3 連続

    cfg = _make_cfg(
        consecutive_unreachable_threshold=3,
        notifier_enabled=True,
    )
    run_mt5_heartbeat(cfg)

    assert len(send_calls) == 1
    assert "SOFT HALT" in send_calls[0]["title"]
    assert "3" in send_calls[0]["description"]


def test_notifier_skipped_when_disabled(tmp_path: Path, monkeypatch):
    """notifier.enabled=False では create_notifier すら呼ばれない。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    _stub_failing_get(monkeypatch)
    _capture_post(monkeypatch)

    create_calls: list = []

    def _spy(enabled):
        create_calls.append(enabled)
        from src.notifications.notifier import NullNotifier
        return NullNotifier()

    monkeypatch.setattr("src.notifications.notifier.create_notifier", _spy)

    _state["consecutive_failures"] = 2

    cfg = _make_cfg(
        consecutive_unreachable_threshold=3,
        notifier_enabled=False,
    )
    run_mt5_heartbeat(cfg)

    assert create_calls == []  # 無効時は通知パス自体スキップ
