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


from src.analysis.audit_report import render_section2_calibration


def test_section2_calibration_buckets():
    """confidence をバケット化した表が生成される。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = []
    # 0.70 帯: 勝ち 2, 負け 1
    sessions.append(make_fake_session("a1", pnl=1000, conf=0.70, opened_at=now))
    sessions.append(make_fake_session("a2", pnl=800, conf=0.72, opened_at=now))
    sessions.append(make_fake_session("a3", pnl=-500, conf=0.74, opened_at=now))
    # 0.80 帯: 負け 2
    sessions.append(make_fake_session("a4", pnl=-1200, conf=0.82, opened_at=now))
    sessions.append(make_fake_session("a5", pnl=-800, conf=0.83, opened_at=now))

    text = render_section2_calibration(sessions)
    assert "## Section 2" in text
    assert "0.70" in text or "0.7" in text
    assert "0.80" in text or "0.8" in text


from src.analysis.audit_report import render_section3_vol_regime, render_section4_time_of_day


def test_section3_vol_regime():
    """vol_percentile マップに基づく regime breakdown。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = [
        make_fake_session(f"v{i}", pnl=1000 if i % 2 == 0 else -500, opened_at=now)
        for i in range(10)
    ]
    vol_map = {f"v{i}": float(i * 10) for i in range(10)}  # 0, 10, 20, ..., 90

    text = render_section3_vol_regime(sessions, vol_map)
    assert "## Section 3" in text
    assert "0-20" in text or "低ボラ" in text
    assert "80-100" in text or "高ボラ" in text


def test_section4_time_of_day():
    """JST hour でバケット化される。"""
    sessions = [
        make_fake_session("t1", pnl=1000,
                          opened_at=datetime(2026, 4, 1, 9, 0)),
        make_fake_session("t2", pnl=-500,
                          opened_at=datetime(2026, 4, 1, 15, 0)),
        make_fake_session("t3", pnl=800,
                          opened_at=datetime(2026, 4, 2, 15, 0)),
        make_fake_session("t4", pnl=1200,
                          opened_at=datetime(2026, 4, 3, 15, 0)),
        make_fake_session("t5", pnl=600,
                          opened_at=datetime(2026, 4, 4, 15, 0)),
    ]
    text = render_section4_time_of_day(sessions)
    assert "## Section 4" in text
    assert "15" in text


from src.analysis.audit_report import render_section5_trade_table


def test_section5_trade_table():
    """全トレードを時系列でテーブル化する。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = [
        make_fake_session("t1", pnl=1000, opened_at=now),
        make_fake_session("t2", pnl=-500, opened_at=now),
    ]
    flags_map = {"t1": "CLEAN_WIN", "t2": "CLEAN_LOSS"}
    text = render_section5_trade_table(sessions, flags_map)
    assert "## Section 5" in text
    assert "CLEAN_WIN" in text
    assert "CLEAN_LOSS" in text
    assert "USDJPY" in text
