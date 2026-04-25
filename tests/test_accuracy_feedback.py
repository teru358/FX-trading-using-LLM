"""Forecast accuracy auto-feedback (signal_combiner + accuracy_tracker) のテスト。

accuracy_provider が低 hit 率を返した場合に signal_combiner が:
  - confidence にペナルティを乗算 (soft_threshold 未満)
  - action="hold" を強制 (hard_threshold 未満)
  - サンプル不足/disabled なら何もしない
を確認する。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.config.schema import ForecastAccuracyFeedbackConfig
from src.signals.accuracy_tracker import AccuracyResult, compute_recent_accuracy
from src.signals.signal_combiner import combine_signals


# ── fixtures ────────────────────────────────────────────────────────────


def _config_enabled(**overrides: Any) -> ForecastAccuracyFeedbackConfig:
    base = dict(
        enabled=True,
        lookback_hours=24,
        min_samples=4,
        soft_threshold=0.50,
        hard_threshold=0.33,
        confidence_penalty=0.7,
    )
    base.update(overrides)
    return ForecastAccuracyFeedbackConfig(**base)


def _provider(result: AccuracyResult | None):
    """単一の固定 result を返す provider。"""
    def _fn(_pair: str) -> AccuracyResult | None:
        return result
    return _fn


def _call(news, price, pair_cfg, **kw):
    return combine_signals(
        news=news, price=price,
        current_price=150.0, pair_cfg=pair_cfg,
        account_balance=100_000.0, risk_per_trade=0.01,
        confidence_threshold=0.55,
        **kw,
    )


# ── compute_recent_accuracy ─────────────────────────────────────────────


@dataclass
class _MockRecord:
    reviewed: int
    latest_price_delta: float | None
    predicted_direction: str


class _MockStore:
    """ForecastStore.get_recent_forecasts のみ持つ最小モック。"""
    def __init__(self, records: list[_MockRecord]) -> None:
        self._records = records

    def get_recent_forecasts(self, pair: str, hours: int = 24) -> list[_MockRecord]:
        return self._records


def test_accuracy_no_records_returns_none():
    store = _MockStore([])
    assert compute_recent_accuracy(store, "USDJPY=X") is None


def test_accuracy_skips_unreviewed():
    """reviewed=0 や delta=None は集計対象外。"""
    store = _MockStore([
        _MockRecord(reviewed=0, latest_price_delta=1.0, predicted_direction="bullish"),
        _MockRecord(reviewed=1, latest_price_delta=None, predicted_direction="bullish"),
    ])
    assert compute_recent_accuracy(store, "USDJPY=X") is None


def test_accuracy_correct_direction_count():
    """bullish 予測 + delta>0 / bearish 予測 + delta<0 を correct と数える。"""
    store = _MockStore([
        _MockRecord(reviewed=1, latest_price_delta=+1.0, predicted_direction="bullish"),  # ✓
        _MockRecord(reviewed=1, latest_price_delta=-2.0, predicted_direction="bearish"),  # ✓
        _MockRecord(reviewed=1, latest_price_delta=+0.5, predicted_direction="bearish"),  # ✗
        _MockRecord(reviewed=1, latest_price_delta=-1.0, predicted_direction="bullish"),  # ✗
    ])
    res = compute_recent_accuracy(store, "USDJPY=X")
    assert res is not None
    assert res.sample_count == 4
    assert res.correct_count == 2
    assert res.accuracy == 0.5


# ── combine_signals: accuracy feedback ─────────────────────────────────


def test_no_provider_no_effect(bullish_news, bullish_price, pair_cfg):
    """accuracy_provider=None なら従来通り (BUY)。"""
    sig = _call(bullish_news, bullish_price, pair_cfg)
    assert sig.action == "buy"


def test_disabled_config_no_effect(bullish_news, bullish_price, pair_cfg):
    """config.enabled=False なら何もしない。"""
    cfg = _config_enabled(enabled=False)
    provider = _provider(AccuracyResult(accuracy=0.10, sample_count=10, correct_count=1))
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=provider, accuracy_config=cfg,
    )
    assert sig.action == "buy"


def test_insufficient_samples_no_effect(bullish_news, bullish_price, pair_cfg):
    """min_samples 未満ならペナルティ適用しない。"""
    cfg = _config_enabled(min_samples=4)
    # 3 サンプル + accuracy 0% でも min_samples 未満なので無視
    provider = _provider(AccuracyResult(accuracy=0.0, sample_count=3, correct_count=0))
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=provider, accuracy_config=cfg,
    )
    assert sig.action == "buy"


def test_high_accuracy_no_effect(bullish_news, bullish_price, pair_cfg):
    """soft_threshold 以上なら何もしない。"""
    cfg = _config_enabled()
    provider = _provider(AccuracyResult(accuracy=0.75, sample_count=8, correct_count=6))
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=provider, accuracy_config=cfg,
    )
    assert sig.action == "buy"


def test_soft_threshold_applies_confidence_penalty(bullish_news, bullish_price, pair_cfg):
    """soft_threshold 未満で confidence × penalty が適用される。"""
    cfg = _config_enabled(soft_threshold=0.50, hard_threshold=0.30, confidence_penalty=0.7)
    provider = _provider(AccuracyResult(accuracy=0.40, sample_count=10, correct_count=4))

    sig_no_penalty = _call(bullish_news, bullish_price, pair_cfg)
    sig_with_penalty = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=provider, accuracy_config=cfg,
    )
    # confidence は 0.7 倍に減衰している
    assert sig_with_penalty.confidence == pytest.approx(sig_no_penalty.confidence * 0.7, abs=0.01)
    # detail_reason に accuracy_note が含まれる
    assert "forecast accuracy 40%" in sig_with_penalty.detail_reason
    assert "conf×0.7" in sig_with_penalty.detail_reason


def test_hard_threshold_forces_hold(bullish_news, bullish_price, pair_cfg):
    """hard_threshold 未満で action="hold" 強制。"""
    cfg = _config_enabled(hard_threshold=0.33)
    provider = _provider(AccuracyResult(accuracy=0.20, sample_count=10, correct_count=2))
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=provider, accuracy_config=cfg,
    )
    assert sig.action == "hold"
    assert "forecast accuracy" in sig.signal_reason
    assert "forced HOLD" in sig.detail_reason


def test_provider_exception_does_not_break_signal(bullish_news, bullish_price, pair_cfg):
    """provider が例外を投げても signal は止まらない (best effort)。"""
    cfg = _config_enabled()
    def _broken(_pair):
        raise RuntimeError("DB connection lost")
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=_broken, accuracy_config=cfg,
    )
    assert sig.action == "buy"  # 通常通りシグナル決定される


def test_provider_returns_none_no_effect(bullish_news, bullish_price, pair_cfg):
    """provider が None (=サンプル無し) を返したら何もしない。"""
    cfg = _config_enabled()
    sig = _call(
        bullish_news, bullish_price, pair_cfg,
        accuracy_provider=_provider(None), accuracy_config=cfg,
    )
    assert sig.action == "buy"
