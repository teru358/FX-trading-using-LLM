"""取引サイクル集約通知の配線テスト (分析 Phase / 実行 Phase / halt サマリー)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_pair_analysis_outcome_and_error_construct():
    from src.cycles.trading import PairAnalysisError, PairAnalysisOutcome

    out = PairAnalysisOutcome(signal=MagicMock(), macro_ctx="m")
    assert out.tech_fallback is False
    err = PairAnalysisError(pair="USDJPY=X", error=RuntimeError("x"))
    assert err.pair == "USDJPY=X"


@pytest.mark.asyncio
async def test_phase_analyze_pairs_collects_data_health(monkeypatch):
    from src.cycles.trading import (
        PairAnalysisError,
        PairAnalysisOutcome,
        _phase_analyze_pairs,
    )

    sig_ok = MagicMock(pair="USDJPY=X")

    async def fake_process(pair_cfg, *a, **k):
        if pair_cfg.symbol == "USDJPY=X":
            return PairAnalysisOutcome(signal=sig_ok, macro_ctx="m", tech_fallback=True)
        return PairAnalysisError(pair="EURUSD=X", error=RuntimeError("boom"))

    monkeypatch.setattr("src.cycles.trading._process_pair", fake_process)

    config = MagicMock()
    config.llm.provider_config.max_concurrent = 2
    config.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X"), MagicMock(symbol="EURUSD=X"),
    ]
    signals, macro_ctxs, data_health = await _phase_analyze_pairs(
        config, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), None,
    )
    assert signals == [sig_ok]
    assert macro_ctxs == {"USDJPY=X": "m"}
    assert any("EURUSD=X 分析失敗" in d for d in data_health)
    assert any("USDJPY=X" in d and "fallback" in d for d in data_health)
