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
        state_dir: Path = field(default_factory=lambda: Path("data/state"))
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


def test_third_consecutive_failure_triggers_halt(tmp_path: Path, monkeypatch):
    """3 連続失敗で /admin/halt を呼ぶ + 以降は再発動しない。"""
    from src.persistence import halt_state

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
    assert halt_state.is_halted(tmp_path / "data/state")

    # 4 回目以降の失敗でも再発動しない
    run_mt5_heartbeat(cfg)
    assert len(posts) == 1


def test_success_resets_failure_counter(tmp_path: Path, monkeypatch):
    """成功プローブで連続失敗カウンタがリセットされる。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    class _Resp:
        is_success = True
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    posts = _capture_post(monkeypatch)

    _state["consecutive_failures"] = 5

    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    assert _state["consecutive_failures"] == 0
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
    with caplog.at_level("WARNING"):
        run_mt5_heartbeat(cfg)

    assert any(
        "bridge halt POST failed" in r.message
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


# ── Task 7: /account fetch + balance.json sync (paper → mt5 transition) ──


def test_health_success_fetches_account_and_updates_balance(tmp_path: Path, monkeypatch):
    """/health 成功時に /account も叩き、balance.json を MT5 値で更新する。

    paper → mt5 切替も同時に検証 (deposit が MT5 値で確定)。
    """
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)

    from src.persistence import balance_snapshot

    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.jobs.mt5_heartbeat._state_dir_for",
        lambda config: state_dir,
    )

    class _Resp:
        def __init__(self, payload, ok=True, status=200):
            self._payload = payload
            self.is_success = ok
            self.status_code = status

        def raise_for_status(self):
            if not self.is_success:
                raise httpx.HTTPStatusError("err", request=None, response=None)

        def json(self):
            return self._payload

    def _get(url, *a, **kw):
        if url.endswith("/health"):
            return _Resp({}, ok=True, status=200)
        if url.endswith("/account"):
            return _Resp({
                "balance": 49823.0, "equity": 50100.0,
                "free_margin": 43000.0, "margin": 6432.0,
            })
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(httpx, "get", _get)

    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    snap = balance_snapshot.read(state_dir)
    assert snap.source == "mt5"
    assert snap.balance == 49823.0
    assert snap.deposit == 49823.0  # 初回 paper → mt5 で確定


def test_account_fetch_failure_keeps_balance_unchanged(tmp_path: Path, monkeypatch):
    """/health OK + /account fail → balance.json 据え置き、soft halt なし。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    state_dir = tmp_path / "data" / "state"
    monkeypatch.setattr(
        "src.jobs.mt5_heartbeat._state_dir_for", lambda config: state_dir,
    )
    posts = _capture_post(monkeypatch)

    class _OkResp:
        is_success = True
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def _get(url, *a, **kw):
        if url.endswith("/health"):
            return _OkResp()
        raise httpx.ConnectError("/account down")

    monkeypatch.setattr(httpx, "get", _get)

    cfg = _make_cfg()
    run_mt5_heartbeat(cfg)

    # halt は呼ばれない
    assert posts == []
    # balance.json は paper デフォルト (¥10k) のまま (read で bootstrap される)
    from src.persistence import balance_snapshot
    snap = balance_snapshot.read(state_dir)
    assert snap.source == "paper"


def test_paper_to_mt5_transition_sends_discord(tmp_path: Path, monkeypatch):
    """notifier 有効 + 初回 mt5 取得時に paper→mt5 通知が送られる。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.jobs.mt5_heartbeat._state_dir_for", lambda config: state_dir,
    )

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

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.is_success = True
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _get(url, *a, **kw):
        if url.endswith("/health"):
            return _Resp({})
        if url.endswith("/account"):
            return _Resp({"balance": 50000.0})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(httpx, "get", _get)

    cfg = _make_cfg(notifier_enabled=True)
    run_mt5_heartbeat(cfg)

    assert len(send_calls) == 1
    assert "paper → mt5" in send_calls[0]["title"]


def test_subsequent_mt5_fetch_does_not_resend_paper_to_mt5(tmp_path: Path, monkeypatch):
    """既に source=mt5 の状態で /account 取得しても paper→mt5 通知は送らない。"""
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "src.jobs.mt5_heartbeat._state_dir_for", lambda config: state_dir,
    )

    # 既に mt5 source の balance.json を seed
    from src.persistence import balance_snapshot
    from src.persistence.balance_snapshot import BalanceSnapshot
    balance_snapshot.write(state_dir, BalanceSnapshot(
        balance=50000.0, deposit=50000.0, peak_balance=50500.0,
        source="mt5", fetched_at="2026-05-04T00:00:00+00:00",
    ))

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

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.is_success = True
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _get(url, *a, **kw):
        if url.endswith("/health"):
            return _Resp({})
        if url.endswith("/account"):
            return _Resp({"balance": 49800.0})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(httpx, "get", _get)

    cfg = _make_cfg(notifier_enabled=True)
    run_mt5_heartbeat(cfg)

    assert send_calls == []  # 通知なし
    snap = balance_snapshot.read(state_dir)
    assert snap.balance == 49800.0  # 値は更新されている
    assert snap.deposit == 50000.0  # 不変


def test_auto_halt_writes_finance_state_and_notifies_when_bridge_post_fails(
    tmp_path, monkeypatch
):
    """auto halt 発動時、bridge への halt POST が失敗しても finance state は確定し
    Discord 通知が送られる (この仕様の核心)。"""
    from src.persistence import halt_state
    from src.jobs import mt5_heartbeat

    # 既存の _make_cfg は **overrides を Mt5Config に渡すため、
    # consecutive_unreachable_threshold をキー名そのままで指定する。
    cfg = _make_cfg(
        notifier_enabled=True,
        consecutive_unreachable_threshold=2,
    )
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    mt5_heartbeat._reset_state_for_test()

    notify_calls: list[dict] = []

    class _NotifierStub:
        async def send_embed(self, **kwargs):
            notify_calls.append(kwargs)

    def _make_notifier(_enabled):
        return _NotifierStub()

    monkeypatch.setattr(
        "src.notifications.notifier.create_notifier", _make_notifier
    )

    # /health は連続失敗、/admin/halt POST も connect error (bridge 不通のため)
    def _http_post(*args, **kwargs):
        raise httpx.ConnectError("bridge down")

    def _http_get(*args, **kwargs):
        raise httpx.ConnectError("bridge down")

    monkeypatch.setattr("httpx.post", _http_post)
    monkeypatch.setattr("httpx.get", _http_get)

    # threshold=2 → 2 回呼んで halt 発動
    mt5_heartbeat.run_mt5_heartbeat(cfg)
    mt5_heartbeat.run_mt5_heartbeat(cfg)

    # ① finance halt state が確定している
    # _state_dir_for(cfg) は BASE_DIR + cfg.state_dir = tmp_path / "data/state"
    state = halt_state.read(tmp_path / "data/state")
    assert state.soft_halted is True
    assert state.auto_triggered is True
    assert state.triggered_by == "heartbeat"

    # ② Discord 通知が 1 回呼ばれている (bridge POST 失敗にもかかわらず)
    assert len(notify_calls) == 1
    assert "SOFT HALT" in notify_calls[0]["title"]


def test_auto_halt_does_not_double_notify_when_already_halted(
    tmp_path, monkeypatch
):
    """既に halted 中の場合、heartbeat が再度発動しても Discord 通知は出ない。"""
    from src.persistence import halt_state
    from src.jobs import mt5_heartbeat

    # 事前に halt state を「halted」にセット (BASE_DIR/data/state が _state_dir_for の戻り値)
    halt_state.trigger_auto(tmp_path / "data/state", "preexisting", "heartbeat")

    cfg = _make_cfg(
        notifier_enabled=True,
        consecutive_unreachable_threshold=1,
    )
    monkeypatch.setattr("src.jobs.mt5_heartbeat.BASE_DIR", tmp_path)
    mt5_heartbeat._reset_state_for_test()

    notify_calls: list[dict] = []

    class _NotifierStub:
        async def send_embed(self, **kwargs):
            notify_calls.append(kwargs)

    monkeypatch.setattr(
        "src.notifications.notifier.create_notifier",
        lambda _enabled: _NotifierStub(),
    )

    def _err(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.post", _err)
    monkeypatch.setattr("httpx.get", _err)

    mt5_heartbeat.run_mt5_heartbeat(cfg)

    # 既に halted 中だったので Discord 通知は出ない
    assert notify_calls == []
