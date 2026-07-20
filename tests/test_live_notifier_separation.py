"""live 発注通知が shadow 設定・shadow webhook から分離されていること (レビュー High)。

修正前の問題:
  - live 発注結果の唯一の通知経路が ShadowNotifier.notify_live_execution で、
    `shadow_triggered` フラグと `shadow_enabled` マスタースイッチで gate されていた。
  - factory (create_shadow_notifier) は DISCORD_SHADOW_WEBHOOK_URL しか見ないため、
    shadow 通知 OFF / shadow webhook 未設定なら live の約定・拒否・失敗が全て消え、
    shadow webhook 設定済みなら実弾の発注結果が shadow チャンネルへ流れる。

修正後の契約:
  - live 通知は通常の DISCORD_WEBHOOK_URL + NotifierConfig.enabled で制御される。
  - shadow_enabled=False でも live 通知は出る。
  - notification.enabled=False なら live 通知は出ない。
  - shadow 通知は従来どおり shadow_enabled / DISCORD_SHADOW_WEBHOOK_URL で制御。
"""
from __future__ import annotations

import asyncio

import pytest

from src.config.schema import NotifierConfig, OrchestratorNotificationsConfig
from src.notifications.discord_notifier import DiscordNotifier
from src.notifications.notifier import NullNotifier
from src.orchestrator.live_notifier import (
    LiveExecutionInfo,
    LiveNotifier,
    create_live_notifier,
)
from src.orchestrator.shadow_notifier import create_shadow_notifier


class _Spy:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def _info(outcome: str = "executed", **kw) -> LiveExecutionInfo:
    base = dict(pair="USDJPY=X", action="buy", plan_id=7, outcome=outcome)
    base.update(kw)
    return LiveExecutionInfo(**base)


# ── factory: webhook 解決 ────────────────────────────────────


def test_live_notifier_uses_normal_webhook_not_shadow(monkeypatch):
    """live 通知は DISCORD_WEBHOOK_URL を使う (shadow webhook ではない)。"""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/normal")
    monkeypatch.setenv("DISCORD_SHADOW_WEBHOOK_URL", "https://example.test/shadow")

    ln = create_live_notifier(NotifierConfig(enabled=True))

    inner = ln._notifier
    assert isinstance(inner, DiscordNotifier)
    assert inner._url == "https://example.test/normal"


def test_live_notifier_disabled_when_notification_disabled(monkeypatch):
    """notification.enabled=False なら NullNotifier (送信しない)。"""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/normal")

    ln = create_live_notifier(NotifierConfig(enabled=False))

    assert isinstance(ln._notifier, NullNotifier)


# ── gate: shadow フラグから独立していること ────────────────────


@pytest.mark.parametrize("shadow_enabled", [True, False])
def test_live_notification_fires_regardless_of_shadow_enabled(shadow_enabled):
    """shadow_enabled が False でも live 通知は出る (shadow から独立)。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    asyncio.run(ln.notify_live_execution(_info()))

    assert len(spy.sent) == 1, "live 通知が shadow フラグに依存している"


def test_live_notification_suppressed_when_notification_disabled():
    """notification.enabled=False で live 通知が止まる。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=False))

    asyncio.run(ln.notify_live_execution(_info()))

    assert spy.sent == []


def test_live_message_has_no_shadow_prefix():
    """live は実弾。shadow の 🧪 prefix を付けない。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    asyncio.run(ln.notify_live_execution(_info()))

    assert "🧪" not in spy.sent[0]


@pytest.mark.parametrize(
    "outcome,must_contain",
    [
        ("executed", "約定"),
        ("skipped", "スキップ"),
        ("halted", "halt"),
        ("rejected", "拒否"),
        ("failed", "失敗"),
    ],
)
def test_outcome_headlines_preserved(outcome, must_contain):
    """outcome 別の見出しが移設後も維持される (誤読防止)。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    asyncio.run(ln.notify_live_execution(_info(outcome)))

    assert must_contain in spy.sent[0]


def test_non_executed_outcome_never_says_executed():
    """拒否・失敗の文面に「約定」を混ぜない。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    for outcome in ("rejected", "failed", "halted"):
        spy.sent.clear()
        asyncio.run(ln.notify_live_execution(_info(outcome, reason="spread too wide")))
        assert "約定" not in spy.sent[0]
        assert "発注されていません" in spy.sent[0] or "発注せず" in spy.sent[0]


def test_order_id_shown_on_executed():
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    asyncio.run(ln.notify_live_execution(_info("executed", order_id="ORD-42")))

    assert "ORD-42" in spy.sent[0]


def test_long_reason_truncated_under_discord_limit():
    """2000 字制限で無言欠落しないよう切詰める。"""
    spy = _Spy()
    ln = LiveNotifier(spy, NotifierConfig(enabled=True))

    asyncio.run(ln.notify_live_execution(_info("rejected", reason="x" * 5000)))

    assert len(spy.sent[0]) <= 1900


# ── shadow 側が live 通知を持たないこと ─────────────────────────


def test_shadow_notifier_no_longer_handles_live_execution():
    """live 通知は shadow から切り離された (二重送信・誤配線の防止)。"""
    sn = create_shadow_notifier(OrchestratorNotificationsConfig())

    assert not hasattr(sn, "notify_live_execution")


def test_shadow_factory_still_uses_shadow_webhook_only(monkeypatch):
    """shadow は従来どおり shadow webhook のみ (本番へ fallback しない)。"""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/normal")
    monkeypatch.delenv("DISCORD_SHADOW_WEBHOOK_URL", raising=False)

    sn = create_shadow_notifier(OrchestratorNotificationsConfig(shadow_enabled=True))

    assert isinstance(sn._notifier, NullNotifier)
