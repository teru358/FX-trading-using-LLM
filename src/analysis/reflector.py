from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

from src.llm.client import LLMClient
from src.llm.response_parser import extract_json
from src.rag.vector_store import VectorStore

if TYPE_CHECKING:
    from src.trading.position_manager import Order

# embed_fn の型: async (text: str) -> list[float]
EmbedFn = Callable[[str], Coroutine[Any, Any, list[float]]]

logger = logging.getLogger(__name__)

_REFLECTION_PROMPT = """You are an FX trading journal analyst reviewing past trading decisions.

=== Previous Trading Cycle ===
Pair: {pair}
Cycle time: {cycle_time}
Decision: {action}
Entry price: {entry_price}
Stop loss: {stop_loss}
Take profit: {take_profit}
Reasoning at the time: {reasoning}

=== What Actually Happened ===
Price at current cycle: {current_price}
Price change since decision: {price_change:+.5f} ({price_change_pct:+.2f}%)
Elapsed time: {elapsed_hours:.1f} hours

=== Task ===
Write a brief trading journal reflection. Evaluate:
1. Was the directional call correct?
2. What did the analysis get right or wrong?
3. What should be done differently next time?
{user_context}
Return ONLY valid JSON:
{{
  "outcome_summary": "<one sentence: what happened vs expectation>",
  "was_directionally_correct": true|false,
  "lesson": "<one actionable lesson for future trades>",
  "confidence_assessment": "<was the confidence level appropriate?>"
}}"""


@dataclass
class Reflection:
    entry_id: str
    pair: str
    cycle_time: datetime
    action: str
    outcome_summary: str
    was_directionally_correct: bool
    lesson: str
    confidence_assessment: str
    full_text: str
    atr_params_suggestion: dict | None = None


async def generate_reflection(
    pair_cfg,
    previous_action: str,
    previous_entry_price: float,
    previous_stop_loss: float,
    previous_take_profit: float,
    previous_reasoning: str,
    previous_cycle_time: datetime,
    current_price: float,
    llm: LLMClient,
    temperature: float = 0.3,
    user_notes: str = "",
) -> Reflection:
    """LLMで振り返りを生成する。"""
    elapsed_hours = (datetime.now() - previous_cycle_time).total_seconds() / 3600
    price_change = current_price - previous_entry_price
    price_change_pct = (price_change / previous_entry_price) * 100

    user_context = f"=== User's Perspective ===\n{user_notes}" if user_notes else ""

    prompt = _REFLECTION_PROMPT.format(
        pair=pair_cfg.display_name,
        cycle_time=previous_cycle_time.strftime("%Y-%m-%d %H:%M"),
        action=previous_action,
        entry_price=previous_entry_price,
        stop_loss=previous_stop_loss,
        take_profit=previous_take_profit,
        reasoning=previous_reasoning[:300],
        current_price=current_price,
        price_change=price_change,
        price_change_pct=price_change_pct,
        elapsed_hours=elapsed_hours,
        user_context=user_context,
    )

    messages = [{"role": "user", "content": prompt}]
    text = await llm.chat(messages, temperature=temperature)

    try:
        data = extract_json(text)
    except Exception:
        data = {}

    outcome = data.get("outcome_summary", f"Price moved {price_change:+.5f} since {previous_action}")
    lesson = data.get("lesson", "No specific lesson extracted.")
    correct = bool(data.get("was_directionally_correct", False))
    conf_assess = data.get("confidence_assessment", "")

    full_text = (
        f"Cycle: {previous_cycle_time.strftime('%Y-%m-%d %H:%M')} | "
        f"Action: {previous_action} @ {previous_entry_price:.5f} | "
        f"Outcome: {outcome} | Lesson: {lesson}"
    )

    correct_mark = "✓" if correct else "✗"
    logger.info(
        f"[REFLECT] {pair_cfg.display_name}: {correct_mark} {previous_action.upper()} "
        f"entry={previous_entry_price:.5f} → now={current_price:.5f} ({price_change_pct:+.2f}%) | "
        f"{outcome}"
    )
    logger.info(f"[REFLECT] {pair_cfg.display_name}: lesson → {lesson}")
    logger.debug(f"[REFLECT] {pair_cfg.display_name}: confidence_assessment={conf_assess}")

    return Reflection(
        entry_id=f"ref_{pair_cfg.symbol}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        pair=pair_cfg.symbol,
        cycle_time=previous_cycle_time,
        action=previous_action,
        outcome_summary=outcome,
        was_directionally_correct=correct,
        lesson=lesson,
        confidence_assessment=conf_assess,
        full_text=full_text,
    )


async def store_reflection(
    reflection: Reflection,
    store: VectorStore,
    embed_fn: EmbedFn,
    close_reason: str | None = None,
) -> None:
    """振り返りをベクトル化してRAGに蓄積する。"""
    embedding = await embed_fn(reflection.full_text)
    store.upsert_reflection(
        entry_id=reflection.entry_id,
        text=reflection.full_text,
        embedding=embedding,
        pair=reflection.pair,
        cycle_time=reflection.cycle_time,
        action=reflection.action,
        outcome_summary=reflection.outcome_summary,
        lesson=reflection.lesson,
        close_reason=close_reason,
    )
    logger.info(f"[REFLECT] {reflection.pair}: stored to RAG | id={reflection.entry_id}")


_CLOSE_REFLECTION_PROMPT = """You are an FX trading journal analyst reviewing a completed trade.

=== Completed Trade ===
Pair: {pair}
Direction: {direction}
Entry price: {entry_price:.5f}
Stop loss:   {stop_loss:.5f}  (risk: {sl_pips:.1f} pips)
Take profit: {take_profit:.5f}  (target: {tp_pips:.1f} pips)
Planned R:R: {planned_rr:.2f}

=== Actual Outcome ===
Close reason: {close_reason_label}
Close price:  {close_price:.5f}
Realized P&L: {realized_pnl:+.2f}
Trade duration: {duration_hours:.1f} hours
Achieved R:R: {achieved_rr:.2f}

=== Why We Entered ===
{signal_reason}

{macro_context_section}
{entry_analysis_section}
{sltp_analysis_section}
{param_history_section}
=== Task ===
Evaluate this completed trade. Assess:
1. Was the directional call correct? (take_profit = yes, stop_loss = no)
2. Was the SL/TP placement appropriate given what actually happened?
3. If macro context is available: did the macro instruments correctly indicate the direction?
4. Was the news sentiment assessment correct? Did the key themes play out as expected?
5. Was the technical analysis direction correct? Were the key support/resistance levels respected?
6. Based on the SL/TP comparison: should the ATR multipliers be adjusted for this pair?
7. What is the ONE most actionable lesson for future {pair} trades?
{user_context}
Return ONLY valid JSON:
{{
  "outcome_summary": "<one sentence: what happened and the key reason>",
  "was_directionally_correct": true|false,
  "lesson": "<one specific, actionable lesson>",
  "confidence_assessment": "<was the entry timing and risk setup appropriate?>",
  "atr_params_suggestion": {{
    "sl_atr_mult": <new_value or null if no change>,
    "tp_atr_mult": <new_value or null if no change>,
    "reason": "<why this change, or 'no change needed'>"
  }}
}}"""

_CLOSE_REASON_LABELS = {
    "take_profit":   "TAKE PROFIT HIT ✓",
    "stop_loss":     "STOP LOSS HIT ✗",
    "manual":        "MANUAL CLOSE",
    "reversal":      "SIGNAL REVERSAL CLOSE",
    "timeout":       "TIMEOUT CLOSE",
    "profit_lock":   "PROFIT LOCK CLOSE ✓",
    "emergency_stop": "EMERGENCY STOP ✗",
}


async def generate_close_reflection(
    pair_cfg,
    order: "Order",
    llm: LLMClient,
    temperature: float = 0.1,
    user_notes: str = "",
    macro_context_at_entry: str = "",
    entry_analysis: str = "",
    sltp_comparison: str = "",
    param_history: str = "",
) -> Reflection:
    """決済済み Order から確定結果ベースの振り返りを生成する。"""
    close_price = order.close_price or order.entry_price
    close_reason = order.close_reason or "manual"
    realized_pnl = order.realized_pnl or 0.0
    closed_at = order.closed_at or datetime.now()

    duration_hours = (closed_at - order.opened_at).total_seconds() / 3600
    pip_multiplier = 100 if "JPY" in order.pair else 10000

    sl_pips = abs(order.entry_price - order.stop_loss) * pip_multiplier
    tp_pips = abs(order.take_profit - order.entry_price) * pip_multiplier
    planned_rr = tp_pips / sl_pips if sl_pips > 0 else 0.0

    actual_move = abs(close_price - order.entry_price) * pip_multiplier
    achieved_rr = actual_move / sl_pips if sl_pips > 0 else 0.0

    signal_reason = order.signal_reason or "Not recorded."
    close_reason_label = _CLOSE_REASON_LABELS.get(close_reason, close_reason.upper())
    user_context = f"=== User's Perspective ===\n{user_notes}" if user_notes else ""
    macro_ctx = getattr(order, "macro_context_at_entry", "") or macro_context_at_entry or ""
    macro_context_section = (
        f"=== Macro Instruments at Entry ===\n{macro_ctx}\n"
        if macro_ctx else ""
    )
    entry_analysis_section = (
        f"=== Entry Analysis (Full Context) ===\n{entry_analysis}\n"
        if entry_analysis else ""
    )
    sltp_analysis_section = (
        f"=== SL/TP Analysis ===\n{sltp_comparison}\n"
        if sltp_comparison else ""
    )
    param_history_section = (
        f"=== Parameter History (last 3) ===\n{param_history}\n"
        if param_history else ""
    )

    prompt = _CLOSE_REFLECTION_PROMPT.format(
        pair=pair_cfg.display_name,
        direction=order.direction.upper(),
        entry_price=order.entry_price,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        sl_pips=sl_pips,
        tp_pips=tp_pips,
        planned_rr=planned_rr,
        close_reason_label=close_reason_label,
        close_price=close_price,
        realized_pnl=realized_pnl,
        duration_hours=duration_hours,
        achieved_rr=achieved_rr,
        signal_reason=signal_reason,
        macro_context_section=macro_context_section,
        entry_analysis_section=entry_analysis_section,
        sltp_analysis_section=sltp_analysis_section,
        param_history_section=param_history_section,
        user_context=user_context,
    )

    messages = [{"role": "user", "content": prompt}]
    try:
        text = await llm.chat(messages, temperature=temperature)
        data = extract_json(text)
    except Exception as e:
        logger.warning(f"[REFLECT/CLOSE] {pair_cfg.display_name}: LLM failed ({e}), using factual fallback")
        data = {}

    won = close_reason == "take_profit"
    outcome = data.get(
        "outcome_summary",
        f"{close_reason_label} @ {close_price:.5f} | PnL: {realized_pnl:+.2f}",
    )
    lesson = data.get(
        "lesson",
        "Review entry conditions and SL/TP placement for this setup.",
    )
    correct = bool(data.get("was_directionally_correct", won))
    conf_assess = data.get("confidence_assessment", "")
    atr_suggestion = data.get("atr_params_suggestion")

    full_text = (
        f"Closed: {order.opened_at.strftime('%Y-%m-%d %H:%M')} → {closed_at.strftime('%Y-%m-%d %H:%M')} | "
        f"{order.direction.upper()} {pair_cfg.display_name} @ {order.entry_price:.5f} | "
        f"{close_reason_label}: {close_price:.5f} | PnL: {realized_pnl:+.2f} | "
        f"Lesson: {lesson}"
    )

    correct_mark = "✓" if correct else "✗"
    logger.info(
        f"[REFLECT/CLOSE] {pair_cfg.display_name}: {correct_mark} {order.direction.upper()} "
        f"entry={order.entry_price:.5f} → close={close_price:.5f} ({close_reason}) "
        f"PnL={realized_pnl:+.2f} | {outcome}"
    )
    logger.info(f"[REFLECT/CLOSE] {pair_cfg.display_name}: lesson → {lesson}")

    return Reflection(
        entry_id=f"ref_{order.order_id}_close",
        pair=order.pair,
        cycle_time=closed_at,
        action=order.direction,
        outcome_summary=outcome,
        was_directionally_correct=correct,
        lesson=lesson,
        confidence_assessment=conf_assess,
        full_text=full_text,
        atr_params_suggestion=atr_suggestion,
    )
