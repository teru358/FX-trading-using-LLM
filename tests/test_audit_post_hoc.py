"""audit_post_hoc モジュールのテスト。"""
from __future__ import annotations

from datetime import datetime

from src.analysis.audit_post_hoc import (
    Counterfactuals,
    LessonCandidate,
    PostHocResult,
    TradeReview,
)


def test_dataclasses_instantiation():
    """dataclasses が最小引数でインスタンス化できる。"""
    ph = PostHocResult(
        mfe_during_trade=100.0, mae_during_trade=-50.0,
        mfe_after_close_24h=200.0, mae_after_close_24h=-30.0,
        duration_seconds=3600.0, has_post_close_data=True,
    )
    assert ph.has_post_close_data is True

    cf = Counterfactuals(
        tp_plus_0_5_atr_hit=True, tp_plus_0_5_atr_pnl=1500.0,
        tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=800.0,
        sl_minus_0_5_atr_hit=False, sl_minus_0_5_atr_pnl=0.0,
        tighter_sl_would_recover=False,
    )
    assert cf.tp_plus_0_5_atr_hit is True

    lc = LessonCandidate(
        rule_text="test rule",
        rationale="because",
        applicability="all pairs",
    )
    assert lc.hint_used == ""
