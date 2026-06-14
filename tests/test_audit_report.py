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


from src.analysis.audit_report import (
    render_section6_detailed_review,
    select_representative_trades,
)


def test_select_representative_trades_win_loss_mix():
    """勝ち 5 + 敗 5 が選定される、NOISE は除外。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = []
    # 勝ち 7 件
    for i in range(7):
        sessions.append(make_fake_session(f"w{i}", pnl=1000 + i * 100, opened_at=now))
    # 敗 7 件
    for i in range(7):
        sessions.append(make_fake_session(f"l{i}", pnl=-1000 - i * 100, opened_at=now))
    # NOISE (< 500)
    sessions.append(make_fake_session("n1", pnl=100, opened_at=now))

    flags_map = {}
    for s in sessions:
        if "n" in s.session_id:
            flags_map[s.session_id] = "NOISE"
        elif "w" in s.session_id:
            flags_map[s.session_id] = "CLEAN_WIN"
        else:
            flags_map[s.session_id] = "CLEAN_LOSS"

    selected = select_representative_trades(sessions, flags_map)
    sids = [s.session_id for s in selected]
    assert len(selected) == 10
    assert all("n" not in sid for sid in sids)  # NOISE 除外
    # 勝ち 5 + 敗 5
    wins_selected = [s for s in selected if s.realized_pnl > 0]
    losses_selected = [s for s in selected if s.realized_pnl <= 0]
    assert len(wins_selected) == 5
    assert len(losses_selected) == 5


def test_select_representative_trades_noise_fallback():
    """全件 NOISE でも |pnl| 順に補充されレビュー対象が空にならない。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = []
    # 勝ち 6 件 (全て NOISE: |pnl| < 500)
    for i in range(6):
        sessions.append(make_fake_session(f"w{i}", pnl=50 + i * 10, opened_at=now))
    # 敗 6 件 (全て NOISE)
    for i in range(6):
        sessions.append(make_fake_session(f"l{i}", pnl=-50 - i * 10, opened_at=now))

    flags_map = {s.session_id: "NOISE" for s in sessions}

    selected = select_representative_trades(sessions, flags_map)
    assert len(selected) == 10
    wins_selected = [s for s in selected if s.realized_pnl > 0]
    losses_selected = [s for s in selected if s.realized_pnl <= 0]
    assert len(wins_selected) == 5
    assert len(losses_selected) == 5
    # 大きい |pnl| から採用される
    assert wins_selected[0].realized_pnl == 100  # 50 + 5*10
    assert losses_selected[0].realized_pnl == -100


def test_select_representative_trades_partial_fallback():
    """非 NOISE で枠が足りない場合だけ NOISE から補充。"""
    now = datetime(2026, 4, 14, 12, 0)
    sessions = []
    # 非 NOISE 勝ち 2 件
    for i in range(2):
        sessions.append(make_fake_session(f"cw{i}", pnl=2000 + i, opened_at=now))
    # NOISE 勝ち 5 件
    for i in range(5):
        sessions.append(make_fake_session(f"nw{i}", pnl=100 + i * 10, opened_at=now))
    # 非 NOISE 敗 5 件
    for i in range(5):
        sessions.append(make_fake_session(f"cl{i}", pnl=-2000 - i, opened_at=now))

    flags_map = {}
    for s in sessions:
        if s.session_id.startswith("n"):
            flags_map[s.session_id] = "NOISE"
        elif s.realized_pnl > 0:
            flags_map[s.session_id] = "CLEAN_WIN"
        else:
            flags_map[s.session_id] = "CLEAN_LOSS"

    selected = select_representative_trades(sessions, flags_map)
    wins_selected = [s for s in selected if s.realized_pnl > 0]
    losses_selected = [s for s in selected if s.realized_pnl <= 0]
    # 勝ちは 2 件の非 NOISE + 3 件の NOISE フォールバック
    assert len(wins_selected) == 5
    clean_win_ids = {s.session_id for s in wins_selected if s.session_id.startswith("cw")}
    fallback_win_ids = {s.session_id for s in wins_selected if s.session_id.startswith("nw")}
    assert len(clean_win_ids) == 2
    assert len(fallback_win_ids) == 3
    # 敗けはそのまま 5 件 (フォールバック不要)
    assert len(losses_selected) == 5
    assert all(s.session_id.startswith("cl") for s in losses_selected)


def test_section6_renders_entry_analysis_and_postHoc():
    """Section 6 のトレード詳細に analysis_summary と flag が含まれる。"""
    now = datetime(2026, 4, 14, 12, 0)
    s = make_fake_session("r1", pnl=-2000, conf=0.82, opened_at=now)
    s.analysis_summary = "Test entry reasoning here."
    s.reflection_text = "Test reflection here."

    review_map = {"r1": {
        "flag": "CONF_MISS",
        "mfe_during": 100,
        "mae_during": -2000,
        "mfe_after_close": 300,
        "mae_after_close": -500,
        "tp_plus_0_5_atr_pnl": 800,
        "tp_plus_1_0_atr_pnl": 1200,
        "sl_minus_0_5_atr_pnl": -1500,
        "tighter_sl_would_recover": False,
    }}
    text = render_section6_detailed_review([s], review_map, accepted_lessons_map={})
    assert "## Section 6" in text
    assert "Test entry reasoning" in text
    assert "CONF_MISS" in text
    assert "Test reflection" in text
