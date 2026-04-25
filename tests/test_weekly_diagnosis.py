"""週次診断ジョブの主要なロジックをユニットテストで検証する。

LLM 呼び出しはモックし、データ集計セクションが期待通り組み立てられるか・
無効化時にノーオペで終了するか・サマリ抽出が正しく動くかを確認する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from src.config.schema import (
    ForecastAccuracyFeedbackConfig,
    WeeklyDiagnosisConfig,
)
from src.jobs.weekly_diagnosis import (
    _build_accuracy_section,
    _build_existing_features_section,
    _build_settings_section,
    _extract_summary_block,
    run_weekly_diagnosis,
)
from src.signals.accuracy_tracker import AccuracyResult


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


# ── _build_accuracy_section ────────────────────────────────────────────


@dataclass
class _Inst:
    symbol: str
    display_name: str


class _StubConfig:
    """tradeable_instruments / watch_only_instruments のみ持つ最小 stub。"""
    def __init__(self, pairs: list[_Inst]) -> None:
        self.tradeable_instruments = pairs
        self.watch_only_instruments = []


def test_accuracy_section_marks_low_accuracy(monkeypatch):
    """soft / hard threshold 未満で警告マーカーが付く。"""
    pairs = [_Inst("USDJPY=X", "USD/JPY"), _Inst("EURUSD=X", "EUR/USD")]

    def fake_compute(_store, sym, hours):
        if sym == "USDJPY=X":
            return AccuracyResult(accuracy=0.20, sample_count=10, correct_count=2)
        return AccuracyResult(accuracy=0.55, sample_count=10, correct_count=5)

    monkeypatch.setattr(
        "src.jobs.weekly_diagnosis.compute_recent_accuracy", fake_compute,
    )
    out = _build_accuracy_section(_StubConfig(pairs), forecast_store=object(), lookback_days=7)
    assert "USD/JPY" in out and "20%" in out and "hard_threshold" in out
    assert "EUR/USD" in out and "55%" in out
    # 50% 以上は警告なし
    assert "EUR/USD (EURUSD=X): 5/10 = 55%" in out


def test_accuracy_section_handles_no_data(monkeypatch):
    pairs = [_Inst("USDJPY=X", "USD/JPY")]
    monkeypatch.setattr(
        "src.jobs.weekly_diagnosis.compute_recent_accuracy",
        lambda *a, **kw: None,
    )
    out = _build_accuracy_section(_StubConfig(pairs), forecast_store=object(), lookback_days=7)
    assert "データ不足" in out


# ── _build_existing_features_section ──────────────────────────────────


def test_existing_features_reflects_feedback_state():
    @dataclass
    class _T:
        forecast_accuracy_feedback: ForecastAccuracyFeedbackConfig

    @dataclass
    class _C:
        trading: _T

    cfg_on = _C(trading=_T(ForecastAccuracyFeedbackConfig(enabled=True)))
    out = _build_existing_features_section(cfg_on)
    assert "有効" in out

    cfg_off = _C(trading=_T(ForecastAccuracyFeedbackConfig(enabled=False)))
    out = _build_existing_features_section(cfg_off)
    assert "無効" in out


# ── _build_settings_section ───────────────────────────────────────────


def test_settings_section_includes_key_params():
    from dataclasses import field as _field

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
        forecast_accuracy_feedback: ForecastAccuracyFeedbackConfig = _field(
            default_factory=ForecastAccuracyFeedbackConfig,
        )

    @dataclass
    class _C:
        trading: _T

    out = _build_settings_section(_C(_T()))
    assert "news_weight" in out
    assert "signal_deadband" in out
    assert "forecast_accuracy_feedback" in out


# ── run_weekly_diagnosis (entry point) ────────────────────────────────


def test_run_weekly_diagnosis_disabled_is_noop(caplog):
    """enabled=False ならノーオペで終了 (例外なし、副作用なし)。"""
    @dataclass
    class _C:
        weekly_diagnosis: WeeklyDiagnosisConfig

    cfg = _C(weekly_diagnosis=WeeklyDiagnosisConfig(enabled=False))
    # 例外を投げず、何も出力しない
    run_weekly_diagnosis(cfg, forecast_store=None)


def test_run_weekly_diagnosis_swallows_exceptions(caplog):
    """ジョブ内で例外が出てもデーモンを止めない (logger.error のみ)。"""
    @dataclass
    class _C:
        weekly_diagnosis: WeeklyDiagnosisConfig

    cfg = _C(weekly_diagnosis=WeeklyDiagnosisConfig(enabled=True))
    # 内部で必ず失敗するよう store を None で渡す → AttributeError 等
    # → run_weekly_diagnosis がキャッチして logger.error するだけで例外は伝播しない
    with caplog.at_level("ERROR"):
        run_weekly_diagnosis(cfg, forecast_store=None)
    assert any("WEEKLY" in r.message for r in caplog.records)
