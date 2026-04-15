"""audit_report のレンダリングテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.analysis.audit_report import render_section1_summary
from tests.fixtures.audit import make_fake_session


def test_section1_summary_normal():
    """標準ケースで期待される行が含まれる。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = [
        make_fake_session(f"s{i}", pnl=1000 if i % 2 == 0 else -500,
                          opened_at=now - timedelta(days=i + 1))
        for i in range(10)
    ]
    text = render_section1_summary(sessions, period_days=30)
    assert "## Section 1: 全体サマリ" in text
    assert "総トレード数" in text
    assert "10" in text
    assert "勝率" in text


def test_section1_summary_empty():
    """トレード 0 件でも crash せず「トレードなし」表示。"""
    text = render_section1_summary([], period_days=30)
    assert "## Section 1: 全体サマリ" in text
    assert "トレードなし" in text or "0" in text


def test_section1_summary_few_trades_warning():
    """トレード 5 件未満で警告表示。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = [make_fake_session(f"s{i}", pnl=500, opened_at=now - timedelta(days=i+1))
                for i in range(3)]
    text = render_section1_summary(sessions, period_days=30)
    assert "統計的に不安定" in text or "warning" in text.lower() or "⚠" in text
