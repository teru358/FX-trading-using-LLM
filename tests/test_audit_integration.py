"""audit 全体の統合テスト。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.analysis.performance_audit import run_audit


@pytest.fixture
def audit_config(tmp_path: Path):
    """最小限の fake config を返す。"""
    cfg = MagicMock()
    cfg.prices_db_path = tmp_path / "prices.db"
    cfg.audit_output_dir = tmp_path / "audit"
    cfg.config_dir = tmp_path / "config"
    cfg.config_dir.mkdir(exist_ok=True)
    cfg.audit_output_dir.mkdir(exist_ok=True)

    inst = MagicMock()
    inst.symbol = "USDJPY=X"
    inst.display_name = "USD/JPY"
    cfg.tradeable_instruments = [inst]
    cfg.watch_only_instruments = []

    cfg.llm = MagicMock()
    cfg.llm.reflection = MagicMock(provider="ollama", model="test", temperature=0.1)

    # trading 設定 — risk_budget (= initial_balance × risk_per_trade) の計算に使う
    cfg.trading = MagicMock()
    cfg.trading.initial_balance = 10000.0
    cfg.trading.risk_per_trade = 0.02

    return cfg


@pytest.fixture
def populated_session_store(audit_config):
    """5 件のクローズ済みセッションを含む session_store を返す。"""
    from src.data.session_store import SessionStore

    store = SessionStore(audit_config.prices_db_path)
    base = datetime.now() - timedelta(days=5)
    for i in range(5):
        sid = f"int{i}"
        opened = base + timedelta(days=i)
        store.create_session(
            session_id=sid,
            pair="USDJPY=X",
            direction="buy",
            entry_price=150.0,
            stop_loss=149.5,
            take_profit=151.0,
            position_size=10000,
            signal_score=0.3,
            signal_confidence=0.7 + i * 0.02,
            macro_context="test",
            analysis_summary=f"entry analysis {i}",
            opened_at=opened,
            atr_value=0.3,
            sl_atr_mult=1.5,
            tp_atr_mult=2.0,
        )
        store.close_session(
            sid,
            closed_at=opened + timedelta(hours=4),
            close_price=151.0 if i % 2 == 0 else 149.5,
            close_reason="take_profit" if i % 2 == 0 else "stop_loss",
            realized_pnl=1000.0 if i % 2 == 0 else -500.0,
        )
    return store


@pytest.fixture
def mock_price_store(monkeypatch):
    """price_store.get_ohlcv を mock して固定 OHLCV を返す。"""
    from src.analysis import performance_audit

    def _fake_ohlcv(pair, since, until=None):
        idx = pd.date_range(start=since, periods=100, freq="1h")
        df = pd.DataFrame({
            "Open": [150.0] * 100,
            "High": [150.5] * 100,
            "Low": [149.5] * 100,
            "Close": [150.0] * 100,
            "Volume": [0] * 100,
        }, index=idx)
        return df

    monkeypatch.setattr(performance_audit, "_load_pair_ohlcv", _fake_ohlcv)


def test_run_audit_non_review_writes_report(audit_config, populated_session_store, mock_price_store):
    """run_audit(review=False) で markdown ファイルが生成される。"""
    result = run_audit(audit_config, days=30, review=False)
    assert result.report_path.exists()
    text = result.report_path.read_text(encoding="utf-8")
    assert "## Section 1" in text
    assert "## Section 5" in text
    assert "## Section 6" in text
    assert result.session_count == 5


def test_run_audit_review_mode_mocks_llm_and_input(
    audit_config, populated_session_store, mock_price_store
):
    """review=True で LLM 候補 mock + input mock → audit_lessons.md に追記。"""
    from src.analysis.audit_post_hoc import LessonCandidate
    from src.analysis import performance_audit as pa

    fake_cands = [LessonCandidate(
        rule_text="test rule",
        rationale="test rationale",
        applicability="all",
    )]

    # audit_config にパスを追加
    audit_config.audit_lessons_path = audit_config.config_dir / "audit_lessons.md"

    # generate_candidates と input を mock
    # side_effect=["1","1","1","1","1"] は losses 分を十分カバー
    with patch("src.analysis.performance_audit.generate_candidates",
               new=AsyncMock(return_value=fake_cands)), \
         patch("src.analysis.performance_audit._default_input_reader",
               side_effect=["1", "1", "1", "1", "1"]):
        result = pa.run_audit(audit_config, days=30, review=True)

    assert result.lessons_added >= 1
    assert audit_config.audit_lessons_path.exists()
    content = audit_config.audit_lessons_path.read_text(encoding="utf-8")
    assert "test rule" in content


def test_cmd_audit_dispatch(audit_config, populated_session_store, mock_price_store, capsys):
    """_cmd_audit が run_audit を呼んでレポートを出力する。"""
    from src.cli import _cmd_audit
    _cmd_audit(audit_config, args=["30"])
    captured = capsys.readouterr()
    assert "Audit" in captured.out or "audit" in captured.out.lower()
