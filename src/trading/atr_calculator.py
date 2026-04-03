"""ATRベースのSL/TP算出。LLM出力値と比較記録を生成し、計算値を優先採用する。"""
from __future__ import annotations
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class SLTPResult:
    computed_sl: float
    computed_tp: float
    llm_sl: float
    llm_tp: float
    adopted: str          # "computed"
    atr_value: float
    sl_atr_mult: float
    tp_atr_mult: float
    key_support: float | None = None
    key_resistance: float | None = None

    def comparison_text(self) -> str:
        return (
            f"ATR(14)={self.atr_value:.5f} sl_mult={self.sl_atr_mult} tp_mult={self.tp_atr_mult}\n"
            f"computed: SL={self.computed_sl:.5f} TP={self.computed_tp:.5f}\n"
            f"llm: SL={self.llm_sl:.5f} TP={self.llm_tp:.5f}\n"
            f"adopted={self.adopted}"
        )

def calculate_sl_tp(
    direction: str, entry_price: float, atr_value: float,
    sl_atr_mult: float, tp_atr_mult: float,
    llm_sl: float, llm_tp: float,
    swing_highs: list[float], swing_lows: list[float],
    key_support: float | None, key_resistance: float | None,
) -> SLTPResult:
    sl_distance = atr_value * sl_atr_mult
    tp_distance = atr_value * tp_atr_mult

    if direction == "buy":
        computed_sl = entry_price - sl_distance
        computed_tp = entry_price + tp_distance
        if key_support is not None and computed_sl < key_support < entry_price:
            adjusted = key_support - atr_value * 0.1
            computed_sl = max(computed_sl, adjusted)
        nearby_lows = [l for l in swing_lows if computed_sl < l < entry_price]
        if nearby_lows:
            adjusted = min(nearby_lows) - atr_value * 0.1
            computed_sl = max(computed_sl, adjusted)
    else:  # sell
        computed_sl = entry_price + sl_distance
        computed_tp = entry_price - tp_distance
        if key_resistance is not None and entry_price < key_resistance < computed_sl:
            adjusted = key_resistance + atr_value * 0.1
            computed_sl = min(computed_sl, adjusted)
        nearby_highs = [h for h in swing_highs if entry_price < h < computed_sl]
        if nearby_highs:
            adjusted = max(nearby_highs) + atr_value * 0.1
            computed_sl = min(computed_sl, adjusted)

    logger.info(
        f"[ATR SL/TP] {direction.upper()} entry={entry_price:.5f} "
        f"ATR={atr_value:.5f}×{sl_atr_mult}/{tp_atr_mult} "
        f"→ SL={computed_sl:.5f} TP={computed_tp:.5f} "
        f"(LLM: SL={llm_sl:.5f} TP={llm_tp:.5f})"
    )
    return SLTPResult(
        computed_sl=computed_sl, computed_tp=computed_tp,
        llm_sl=llm_sl, llm_tp=llm_tp, adopted="computed",
        atr_value=atr_value, sl_atr_mult=sl_atr_mult, tp_atr_mult=tp_atr_mult,
        key_support=key_support, key_resistance=key_resistance,
    )
