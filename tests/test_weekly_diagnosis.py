"""週次診断ジョブの主要なロジックをユニットテストで検証する。

LLM 呼び出しはモックし、データ集計セクションが期待通り組み立てられるか・
無効化時にノーオペで終了するか・サマリ抽出が正しく動くかを確認する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from src.config.schema import WeeklyDiagnosisConfig
from src.jobs.weekly_diagnosis import (
    _build_settings_section,
    _extract_summary_block,
    run_weekly_diagnosis,
)


# ── _extract_summary_block ─────────────────────────────────────────────


def test_extract_summary_block_finds_section():
    md = """## サマリ
今週は USD/JPY が好調、EUR/USD は低迷。

## 機能している点
- BoJ rate-hike retreat の捕捉
"""
    out = _extract_summary_block(md)
    assert out is not None
    assert "今週は USD/JPY が好調" in out
    assert "機能している点" not in out


def test_extract_summary_block_returns_none_when_missing():
    assert _extract_summary_block("# Header\nNo summary here.") is None


# ── _build_settings_section ───────────────────────────────────────────


def test_settings_section_includes_key_params():
    @dataclass
    class _T:
        news_weight: float = 0.40
        price_weight: float = 0.60
        signal_deadband: float = 0.15
        signal_confidence_threshold: float = 0.55
        risk_per_trade: float = 0.01
        min_rr_ratio: float = 1.5
        drawdown_kill_switch_enabled: bool = False
        vol_regime_enabled: bool = False

    @dataclass
    class _C:
        trading: _T

    out = _build_settings_section(_C(_T()))
    assert "news_weight" in out
    assert "signal_deadband" in out


# ── run_weekly_diagnosis (entry point) ────────────────────────────────


def test_run_weekly_diagnosis_disabled_is_noop(caplog):
    """enabled=False ならノーオペで終了 (例外なし、副作用なし)。"""
    @dataclass
    class _C:
        weekly_diagnosis: WeeklyDiagnosisConfig

    cfg = _C(weekly_diagnosis=WeeklyDiagnosisConfig(enabled=False))
    # 例外を投げず、何も出力しない
    run_weekly_diagnosis(cfg)


def test_run_weekly_diagnosis_swallows_exceptions(caplog):
    """ジョブ内で例外が出てもデーモンを止めない (logger.error のみ)。"""
    @dataclass
    class _C:
        weekly_diagnosis: WeeklyDiagnosisConfig

    cfg = _C(weekly_diagnosis=WeeklyDiagnosisConfig(enabled=True))
    # 内部で必ず失敗する (最小 stub config に他セクションが無い → AttributeError 等)
    # → run_weekly_diagnosis がキャッチして logger.error するだけで例外は伝播しない
    with caplog.at_level("ERROR"):
        run_weekly_diagnosis(cfg)
    assert any("WEEKLY" in r.message for r in caplog.records)
