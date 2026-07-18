from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from src.llm.client import LLMClient
from src.llm.response_parser import extract_json
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.trading.position_manager import Order

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

# LLM 応答の必須キーと期待型 (spec §3.5b)。欠落・型不正・明示的 null は失敗扱い。
_REQUIRED_KEY_TYPES: dict[str, type] = {
    "outcome_summary": str,
    "lesson": str,
    "was_directionally_correct": bool,
}

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
    """決済済み Order から確定結果ベースの振り返りを生成する。

    方向正誤 (`was_directionally_correct`) は価格方向による機械判定を正とし、
    LLM の申告値は採用しない (spec §3.5b)。LLM は叙述 (outcome_summary / lesson /
    confidence_assessment) のみを担う。

    fallback は持たない。失敗は呼び出し側 (reflection job) の retry 管理へ委ねる。

    Raises:
        ReflectionValidationError: `order.close_price` が None で方向判定不能な場合、
            または LLM 応答の必須キーが欠落・型不正な場合。
        ValueError: LLM 応答から JSON を抽出できなかった場合 (`extract_json` 由来。
            `json.JSONDecodeError` を含む)。
        Exception: LLM 呼び出し自体が失敗した場合 (クライアント実装依存)。
    """
    # 方向判定は close_price に全面依存するため、entry_price での補填はしない。
    # 補填すると entry > entry = False となり、捏造された「方向的に誤り」が
    # full_text 経由で directional RAG に永続化される (spec §3.5b)。
    if order.close_price is None:
        raise ReflectionValidationError(
            f"close_price is None for {order.order_id}; cannot determine direction"
        )
    close_price = order.close_price
    close_reason = order.close_reason or "manual"
    realized_pnl = order.realized_pnl or 0.0
    closed_at = order.closed_at or db_now()

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
    # extract_json は全 return パスで dict を保証する (src/llm/response_parser.py)。
    # 抽出不能なら ValueError を投げるため、ここでの非 dict チェックは不要。
    data = extract_json(text)

    # 存在チェックと型チェックを分けず、必須キーは「期待型であること」で一括判定する。
    # `k not in data` だけでは明示的 null を通してしまうが (_sanitize_json が壊れた値を
    # 能動的に null へ変換するため現実的な LLM 出力)、型が一致しないので確実に弾ける。
    for key, expected in _REQUIRED_KEY_TYPES.items():
        if key not in data:
            raise ReflectionValidationError(f"missing key: {key}")
        if not isinstance(data[key], expected):
            raise ReflectionValidationError(
                f"{key} must be {expected.__name__}, got {type(data[key]).__name__}"
            )
    # 任意キーだが、存在するなら型は守らせる (Reflection の型注釈を嘘にしない)。
    conf_assess = data.get("confidence_assessment", "")
    if not isinstance(conf_assess, str):
        raise ReflectionValidationError(
            f"confidence_assessment must be str, got {type(conf_assess).__name__}"
        )

    outcome = data["outcome_summary"]
    lesson = data["lesson"]

    # 方向正誤は価格方向の機械判定を正とする (spec §3.5b)。
    # close_reason == "take_profit" 基準は trailing SL / manual の利益決済を誤判定する。
    # 境界: close == entry (建値決済) は厳密不等号により False に倒れる。これは意図的で、
    # 建値で利益が出ていない以上「方向の優位性は確認できなかった」という扱いにする。
    # 三値化しないのは spec §3.4 が done 時の was_directionally_correct を必須と
    # 規定しており、NULL を flat に流用すると衝突するため。
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
