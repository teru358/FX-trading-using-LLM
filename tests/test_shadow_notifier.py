"""ShadowNotifier (Phase 5) のフォーマット・フラグ・分離・分割テスト。

shadow 専用通知は既存 cycle summary と混ざらないこと、各イベントフラグで個別
on/off できること、🧪 prefix を持つこと、daily summary が 2000 字超で pair 分割
されることを検証する。実 Discord は叩かず、send() を記録する fake notifier を使う。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config.schema import OrchestratorNotificationsConfig
from src.notifications.notifier import NotifierAdapter
from src.orchestrator.shadow_metrics import ShadowMetrics
from src.orchestrator.shadow_notifier import (
    PlanCreatedInfo,
    ShadowTriggerInfo,
    HindsightInfo,
    ShadowNotifier,
)


class FakeNotifier(NotifierAdapter):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _trigger() -> ShadowTriggerInfo:
    return ShadowTriggerInfo(
        pair="USDJPY=X", direction="long", plan_id=42,
        score=0.71, confidence=0.68,
        trigger_price=160.120, sl=159.420, tp=161.520, rr=2.0,
        reason="pullback entry after technical recovery",
    )


@pytest.mark.asyncio
async def test_shadow_trigger_has_emoji_prefix_and_fields() -> None:
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    await sn.notify_shadow_trigger(_trigger())
    assert len(fake.messages) == 1
    msg = fake.messages[0]
    assert msg.startswith("🧪")
    assert "Shadow trigger" in msg
    assert "USDJPY=X" in msg
    assert "LONG" in msg
    assert "plan=42" in msg
    assert "RR 2.0" in msg
    assert "pullback entry" in msg


@pytest.mark.asyncio
async def test_event_flag_off_suppresses_notification() -> None:
    fake = FakeNotifier()
    cfg = OrchestratorNotificationsConfig(shadow_triggered=False)
    sn = ShadowNotifier(fake, cfg)
    await sn.notify_shadow_trigger(_trigger())
    assert fake.messages == []


@pytest.mark.asyncio
async def test_master_switch_off_suppresses_all() -> None:
    fake = FakeNotifier()
    cfg = OrchestratorNotificationsConfig(shadow_enabled=False)
    sn = ShadowNotifier(fake, cfg)
    await sn.notify_shadow_trigger(_trigger())
    await sn.notify_plan_created(
        PlanCreatedInfo(pair="USDJPY=X", direction="long", plan_id=1,
                        score=0.5, confidence=0.5, reason="x")
    )
    assert fake.messages == []


@pytest.mark.asyncio
async def test_plan_created_and_rejected_and_superseded() -> None:
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    info = PlanCreatedInfo(
        pair="EURUSD=X", direction="short", plan_id=7,
        score=0.6, confidence=0.55, reason="breakdown setup",
    )
    await sn.notify_plan_created(info)
    await sn.notify_plan_rejected(pair="EURUSD=X", reason="risk reject: spread")
    await sn.notify_plan_superseded(pair="EURUSD=X", old_plan_id=7, new_plan_id=9)
    assert len(fake.messages) == 3
    assert all(m.startswith("🧪") for m in fake.messages)
    assert "Plan created" in fake.messages[0] and "plan=7" in fake.messages[0]
    assert "Plan rejected" in fake.messages[1]
    assert "superseded" in fake.messages[2].lower()


@pytest.mark.asyncio
async def test_plan_created_flag_gates_create_not_reject() -> None:
    """plan_created フラグは created のみ抑制し、rejected/superseded は別フラグ扱いしない
    (この 3 種は plan ライフサイクルとして shadow_plan_created で一括 gate)。"""
    fake = FakeNotifier()
    cfg = OrchestratorNotificationsConfig(shadow_plan_created=False)
    sn = ShadowNotifier(fake, cfg)
    await sn.notify_plan_created(
        PlanCreatedInfo(pair="EURUSD=X", direction="short", plan_id=7,
                        score=0.6, confidence=0.55, reason="x")
    )
    await sn.notify_plan_rejected(pair="EURUSD=X", reason="y")
    assert fake.messages == []


@pytest.mark.asyncio
async def test_hindsight_notification() -> None:
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    await sn.notify_hindsight_evaluated(
        HindsightInfo(
            pair="USDJPY=X", direction="long", plan_id=42,
            mfe_r=1.8, mae_r=-0.4, pnl_r=2.0,
            would_hit_tp=True, would_hit_sl=False,
        )
    )
    assert len(fake.messages) == 1
    msg = fake.messages[0]
    assert msg.startswith("🧪")
    assert "Hindsight" in msg
    assert "PnL" in msg and "2.0" in msg
    assert "MFE" in msg


@pytest.mark.asyncio
async def test_daily_summary_basic() -> None:
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    metrics = ShadowMetrics(
        plans_created=10, plans_triggered=4, plans_invalidated=3,
        plans_expired=1, plans_superseded=2, trigger_rate=0.4,
        hindsight_evaluated=4, avg_mfe_r=1.2, avg_mae_r=-0.5, avg_pnl_r=0.8,
        tp_hit_rate=0.5, sl_hit_rate=0.25,
    )
    day = datetime(2026, 6, 21, tzinfo=timezone.utc)
    await sn.notify_daily_summary(metrics, day=day)
    assert len(fake.messages) == 1
    msg = fake.messages[0]
    assert msg.startswith("🧪")
    assert "Daily" in msg or "daily" in msg
    assert "10" in msg  # plans_created
    assert "trigger" in msg.lower()


@pytest.mark.asyncio
async def test_daily_summary_does_not_use_cycle_summary_path() -> None:
    """shadow daily summary は CycleSummaryEvent を作らず send() に直接書く
    (既存 notify_cycle_summary 経路と混ざらないことの担保)。"""
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    await sn.notify_daily_summary(ShadowMetrics(), day=datetime(2026, 6, 21))
    # cycle summary の見出し (取引サイクル) を含まない
    assert "取引サイクル" not in fake.messages[0]


@pytest.mark.asyncio
async def test_long_message_split_under_2000() -> None:
    """2000 字を超える内容は複数メッセージに分割し、各メッセージは 2000 字以内。"""
    fake = FakeNotifier()
    sn = ShadowNotifier(fake, OrchestratorNotificationsConfig())
    # 各 pair 1 行で大量の per-pair 行を渡し、合計が 2000 字を超えるようにする。
    lines = [f"pair{i}=X: created=5 triggered=2 pnl_r=+0.50" for i in range(120)]
    await sn._send_chunked("🧪 Shadow daily 2026-06-21", lines)
    assert len(fake.messages) >= 2
    assert all(len(m) <= 2000 for m in fake.messages)
    # 全行が送られている
    joined = "\n".join(fake.messages)
    assert "pair0=X" in joined and "pair119=X" in joined
