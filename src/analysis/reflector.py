from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

from src.llm.client import LLMClient
from src.llm.response_parser import extract_json

if TYPE_CHECKING:
    from src.trading.position_manager import Order

# embed_fn の型: async (text: str) -> list[float]
EmbedFn = Callable[[str], Coroutine[Any, Any, list[float]]]

logger = logging.getLogger(__name__)


class ReflectionValidationError(Exception):
    """LLM 応答が必須スキーマを満たさない (spec §3.5b)。retry 管理へ乗せる。"""


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


_CLOSE_REFLECTION_SYSTEM = (
    "You are an FX trading journal analyst reviewing a completed trade. "
    "Compare planned vs actual R:R, evaluate entry timing and SL/TP placement, and distill "
    "one actionable lesson. Output ONLY valid JSON, no markdown fences, no commentary "
    "outside the JSON object."
)

_CLOSE_REFLECTION_PROMPT = """=== Completed Trade ===
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
Achieved R:R: {achieved_rr:+.2f}

=== Why We Entered ===
{signal_reason}

{entry_analysis_section}
=== Task ===
Evaluate this completed trade. Assess:
1. Was the directional call correct? (did price move in the traded direction?)
2. Was the SL/TP placement appropriate given what actually happened?
3. What is the ONE most actionable lesson for future {pair} trades?
{user_context}
Return ONLY valid JSON:
{{
  "outcome_summary": "<one sentence: what happened and the key reason>",
  "was_directionally_correct": true|false,
  "lesson": "<one specific, actionable lesson>",
  "confidence_assessment": "<was the entry timing and risk setup appropriate?>"
}}"""

# LLM 応答の必須キー (spec §3.5b)。欠落・型不正は失敗扱いにする。
_REQUIRED_KEYS = ("outcome_summary", "lesson", "was_directionally_correct")

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
    entry_analysis: str = "",
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

    if order.direction == "buy":
        actual_move = (close_price - order.entry_price) * pip_multiplier
    else:
        actual_move = (order.entry_price - close_price) * pip_multiplier
    achieved_rr = actual_move / sl_pips if sl_pips > 0 else 0.0

    signal_reason = order.signal_reason or "Not recorded."
    close_reason_label = _CLOSE_REASON_LABELS.get(close_reason, close_reason.upper())
    user_context = f"=== User's Perspective ===\n{user_notes}" if user_notes else ""
    entry_analysis_section = (
        f"=== Entry Analysis (Full Context) ===\n{entry_analysis}\n"
        if entry_analysis else ""
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
        entry_analysis_section=entry_analysis_section,
        user_context=user_context,
    )

    messages = [
        {"role": "system", "content": _CLOSE_REFLECTION_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    # fallback は持たない: LLM 失敗も schema 不正も例外を伝搬させ、呼び出し側
    # (reflection job) の retry 管理に委ねる (spec §3.5)。
    text = await llm.chat(messages, temperature=temperature)
    data = extract_json(text)

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ReflectionValidationError(f"missing keys: {missing}")
    if not isinstance(data["was_directionally_correct"], bool):
        raise ReflectionValidationError("was_directionally_correct must be bool")
    if not isinstance(data["outcome_summary"], str) or not isinstance(data["lesson"], str):
        raise ReflectionValidationError("outcome_summary/lesson must be str")

    outcome = data["outcome_summary"]
    lesson = data["lesson"]
    conf_assess = data.get("confidence_assessment", "")

    # 方向正誤は価格方向の機械判定を正とする (spec §3.5b)。
    # close_reason == "take_profit" 基準は trailing SL / manual の利益決済を誤判定する。
    if order.direction == "buy":
        correct = close_price > order.entry_price
    else:
        correct = close_price < order.entry_price
    # LLM 申告は叙述の整合確認に使う (spec §3.5b)。不一致は lesson が逆方向の
    # 解釈を含む可能性があるため warning を残す。
    if data["was_directionally_correct"] != correct:
        logger.warning(
            f"[REFLECT/CLOSE] {order.order_id}: LLM directional claim "
            f"({data['was_directionally_correct']}) != machine verdict "
            f"({correct}) — using machine verdict"
        )

    full_text = (
        f"Closed: {order.opened_at.strftime('%Y-%m-%d %H:%M')} → {closed_at.strftime('%Y-%m-%d %H:%M')} | "
        f"{order.direction.upper()} {pair_cfg.display_name} @ {order.entry_price:.5f} | "
        f"{close_reason_label}: {close_price:.5f} | PnL: {realized_pnl:+.2f} | "
        f"directionally_correct={correct} | "
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
    )
