"""live 発注結果の Discord 通知 (外部レビュー Medium)。

Task 8 で旧 notify_order_opened / notify_signal_skipped を削除した結果、live 発注の
約定・拒否・失敗を Discord で確認する経路が消えた。さらに live でも "Shadow trigger"
通知だけが飛び、拒否されても発注されたと誤読される。

ここでは以下を検証する:
  - outcome 5 種で live 通知が飛び、文面が outcome を取り違えないこと
  - order_id / broker 理由が文面に載ること
  - shadow trigger 通知は shadow モード限定 (live では出ない)
"""
from __future__ import annotations

from src.orchestrator.shadow_notifier import LiveExecutionInfo, ShadowNotifier
from src.trading.broker_adapter import ExecutionResult

from tests.test_taskf_live_execution_helpers import (
    NOW,
    _FakeBroker,
    _GatePass,
    _GateReject,
    _executed_order,
    make_live_runtime,
    seed_active_plan_ready_to_trigger,
)


class RecordingNotifier:
    """runtime に注入する duck-typed スパイ。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def notify_plan_created(self, info) -> None:
        self.events.append(("plan_created", info))

    async def notify_plan_rejected(self, *, pair, reason) -> None:
        self.events.append(("plan_rejected", (pair, reason)))

    async def notify_plan_superseded(self, *, pair, old_plan_id, new_plan_id) -> None:
        self.events.append(("plan_superseded", None))

    async def notify_shadow_trigger(self, info) -> None:
        self.events.append(("shadow_trigger", info))

    async def notify_live_execution(self, info) -> None:
        self.events.append(("live_execution", info))

    async def notify_hindsight_evaluated(self, info) -> None:
        self.events.append(("hindsight", info))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def first(self, kind: str):
        for k, v in self.events:
            if k == kind:
                return v
        return None


class _CapturingSend:
    """ShadowNotifier に渡す NotifierAdapter スタブ。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def _notif_config():
    from src.config.schema import OrchestratorNotificationsConfig

    return OrchestratorNotificationsConfig()


# ── runtime 配線 ────────────────────────────────────────────────


def _run_live(tmp_path, result, gate=None):
    broker = _FakeBroker(result)
    rt = make_live_runtime(tmp_path, broker, gate or _GatePass())
    notifier = RecordingNotifier()
    rt._notifier = notifier
    plan_id = seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    return rt, notifier, plan_id


def test_live_executed_emits_live_notification(tmp_path):
    _, notifier, plan_id = _run_live(
        tmp_path, ExecutionResult.executed(_executed_order()))
    assert "live_execution" in notifier.kinds()
    info = notifier.first("live_execution")
    assert info.outcome == "executed"
    assert info.order_id == "mt5:999"
    assert info.plan_id == plan_id
    assert info.pair == "USDJPY=X"
    assert info.action == "buy"


def test_live_rejected_emits_live_notification_with_reason(tmp_path):
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.rejected("broker: invalid stops"))
    info = notifier.first("live_execution")
    assert info is not None
    assert info.outcome == "rejected"
    assert info.order_id is None
    assert "invalid stops" in info.reason


def test_live_failed_emits_live_notification_with_reason(tmp_path):
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.failed("connection lost"))
    info = notifier.first("live_execution")
    assert info is not None
    assert info.outcome == "failed"
    assert "connection lost" in info.reason


def test_live_skipped_emits_live_notification(tmp_path):
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.skipped("already in position"))
    info = notifier.first("live_execution")
    assert info is not None
    assert info.outcome == "skipped"


def test_live_halted_emits_live_notification(tmp_path):
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.halted("daily loss limit"))
    info = notifier.first("live_execution")
    assert info is not None
    assert info.outcome == "halted"


def test_live_gate_reject_emits_live_notification(tmp_path):
    """broker まで届かない gate reject も運用者に届くこと。"""
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.executed(_executed_order()),
        gate=_GateReject("structural"))
    info = notifier.first("live_execution")
    assert info is not None
    assert info.outcome == "rejected"


def test_shadow_trigger_notification_suppressed_in_live_mode(tmp_path):
    """live で "Shadow trigger" が出ないこと (発注済みとの誤読を防ぐ)。"""
    _, notifier, _ = _run_live(
        tmp_path, ExecutionResult.executed(_executed_order()))
    assert "shadow_trigger" not in notifier.kinds()


def test_shadow_mode_still_emits_shadow_trigger(tmp_path):
    """shadow モードでは従来どおり shadow trigger 通知が出る (退行防止)。"""
    rt = make_live_runtime(tmp_path, None, _GatePass(), mode="shadow")
    notifier = RecordingNotifier()
    rt._notifier = notifier
    seed_active_plan_ready_to_trigger(rt)
    rt.run_watch_cycle(now=NOW)
    assert "shadow_trigger" in notifier.kinds()
    assert "live_execution" not in notifier.kinds()


# ── ShadowNotifier の文面 ────────────────────────────────────────


def _fmt(outcome, *, order_id=None, reason=""):
    import asyncio

    send = _CapturingSend()
    n = ShadowNotifier(send, _notif_config())
    info = LiveExecutionInfo(
        pair="USDJPY=X", action="buy", plan_id=7, outcome=outcome,
        order_id=order_id, reason=reason,
    )
    asyncio.run(n.notify_live_execution(info))
    assert len(send.sent) == 1
    return send.sent[0]


def test_executed_message_says_executed_and_has_order_id():
    msg = _fmt("executed", order_id="mt5:999")
    assert "mt5:999" in msg
    assert "USDJPY=X" in msg
    assert "BUY" in msg.upper()


def test_rejected_message_does_not_read_as_executed():
    msg = _fmt("rejected", reason="invalid stops")
    low = msg.lower()
    assert "invalid stops" in msg
    # 「発注された」と誤読させる語を含まないこと
    assert "executed" not in low
    assert "約定" not in msg
    assert "shadow trigger" not in low


def test_failed_message_does_not_read_as_executed():
    msg = _fmt("failed", reason="connection lost")
    low = msg.lower()
    assert "connection lost" in msg
    assert "executed" not in low
    assert "約定" not in msg


def test_skipped_and_halted_messages_are_distinct():
    a = _fmt("skipped", reason="already in position")
    b = _fmt("halted", reason="daily loss limit")
    assert a != b
    for m in (a, b):
        assert "executed" not in m.lower()
        assert "約定" not in m


def test_live_notification_is_not_prefixed_as_shadow():
    """live 通知に 🧪 (shadow prefix) が付かないこと。"""
    msg = _fmt("executed", order_id="mt5:1")
    assert "🧪" not in msg


def test_live_notification_gated_by_flag():
    import asyncio

    from src.config.schema import OrchestratorNotificationsConfig

    send = _CapturingSend()
    cfg = OrchestratorNotificationsConfig(shadow_enabled=False)
    n = ShadowNotifier(send, cfg)
    info = LiveExecutionInfo(
        pair="USDJPY=X", action="buy", plan_id=1, outcome="executed",
        order_id="x", reason="",
    )
    asyncio.run(n.notify_live_execution(info))
    assert send.sent == []
