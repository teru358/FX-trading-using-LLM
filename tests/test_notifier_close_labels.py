"""Notifier.notify_order_closed: close_reason → ラベルマッピングの網羅テスト。

2026-05-14 incident: profit_lock / server_sl_tp 等が「手動決済」と誤表示された
notifier の分岐漏れを固定する。
"""
from __future__ import annotations

import asyncio
import os
import pytest

from src.notifications.notifier import OrderClosedEvent, create_notifier


@pytest.fixture
def discord_webhook_url(monkeypatch):
    """Discord webhook URL を環境変数に設定し、テスト終了後に復元する。"""
    old_url = os.environ.get("DISCORD_WEBHOOK_URL")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/dummy")
    yield
    if old_url is not None:
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", old_url)
    else:
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


def _make_event(close_reason: str) -> OrderClosedEvent:
    return OrderClosedEvent(
        pair="USDJPY=X",
        direction="buy",
        entry_price=150.0,
        close_price=151.0,
        realized_pnl=100.0,
        close_reason=close_reason,
        balance=50_000.0,
        source="exit_check",
    )


@pytest.mark.parametrize(
    "close_reason, expected_label",
    [
        ("take_profit",    "TP到達"),
        ("stop_loss",      "SL到達"),
        ("emergency_stop", "緊急損切り"),
        ("profit_lock",    "利益確定 (L3 profit_lock)"),
        ("reversal",       "反転シグナル (L1 reversal)"),
        ("timeout",        "保有期間超過 (L2 timeout)"),
        ("timeout_no_progress", "進捗不足による撤退 (L2 timeout)"),
        ("timeout_stale_position", "長時間保有レビューによる撤退 (L2 timeout)"),
        ("server_sl_tp",   "MT5サーバー側決済 (reconciliation検知)"),
        ("server_sl_tp_estimated", "MT5サーバー側決済 (reconciliation検知・損益推定)"),
        ("manual",         "手動決済"),
    ],
)
def test_notify_order_closed_label_mapping(close_reason, expected_label, monkeypatch, discord_webhook_url):
    """各 close_reason が期待ラベルにマップされる。"""
    sent_messages: list[str] = []

    notifier = create_notifier(enabled=True)

    async def _capture(msg: str) -> None:
        sent_messages.append(msg)

    monkeypatch.setattr(notifier, "send", _capture)

    asyncio.run(notifier.notify_order_closed(_make_event(close_reason)))

    assert len(sent_messages) == 1
    assert expected_label in sent_messages[0], (
        f"close_reason={close_reason!r}: expected label "
        f"{expected_label!r} not in {sent_messages[0]!r}"
    )


def test_notify_order_closed_unknown_reason_includes_raw_value(monkeypatch, discord_webhook_url):
    """未知の close_reason は raw 値を含むフォールバックラベルにする。"""
    sent_messages: list[str] = []

    notifier = create_notifier(enabled=True)

    async def _capture(msg: str) -> None:
        sent_messages.append(msg)

    monkeypatch.setattr(notifier, "send", _capture)

    asyncio.run(notifier.notify_order_closed(_make_event("future_new_reason")))

    assert len(sent_messages) == 1
    # 「その他」または raw 値が出ることを確認
    assert "future_new_reason" in sent_messages[0] or "その他" in sent_messages[0]
