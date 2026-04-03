from __future__ import annotations
import pytest
from src.trading.atr_calculator import calculate_sl_tp, SLTPResult

def test_long_basic():
    result = calculate_sl_tp(direction="buy", entry_price=1.1500, atr_value=0.0050,
        sl_atr_mult=1.5, tp_atr_mult=3.0, llm_sl=1.1480, llm_tp=1.1560,
        swing_highs=[1.1560], swing_lows=[1.1420], key_support=None, key_resistance=None)
    assert result.computed_sl == pytest.approx(1.1425, abs=0.0001)
    assert result.computed_tp == pytest.approx(1.1650, abs=0.0001)
    assert result.adopted == "computed"

def test_short_basic():
    result = calculate_sl_tp(direction="sell", entry_price=1.1500, atr_value=0.0050,
        sl_atr_mult=1.5, tp_atr_mult=3.0, llm_sl=1.1520, llm_tp=1.1440,
        swing_highs=[1.1580], swing_lows=[1.1420], key_support=None, key_resistance=None)
    assert result.computed_sl == pytest.approx(1.1575, abs=0.0001)
    assert result.computed_tp == pytest.approx(1.1350, abs=0.0001)

def test_support_adjustment_long():
    result = calculate_sl_tp(direction="buy", entry_price=1.1500, atr_value=0.0050,
        sl_atr_mult=1.5, tp_atr_mult=3.0, llm_sl=1.1480, llm_tp=1.1560,
        swing_highs=[], swing_lows=[], key_support=1.1460, key_resistance=None)
    assert result.computed_sl <= 1.1460

def test_resistance_adjustment_short():
    result = calculate_sl_tp(direction="sell", entry_price=1.1500, atr_value=0.0050,
        sl_atr_mult=1.5, tp_atr_mult=3.0, llm_sl=1.1520, llm_tp=1.1440,
        swing_highs=[], swing_lows=[], key_support=None, key_resistance=1.1540)
    assert result.computed_sl >= 1.1540

def test_comparison_text():
    result = calculate_sl_tp(direction="buy", entry_price=1.1500, atr_value=0.0050,
        sl_atr_mult=1.5, tp_atr_mult=3.0, llm_sl=1.1480, llm_tp=1.1560,
        swing_highs=[], swing_lows=[], key_support=None, key_resistance=None)
    text = result.comparison_text()
    assert "ATR" in text and "computed" in text and "llm" in text
