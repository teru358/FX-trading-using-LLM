from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.llm.client import LLMClient
from src.llm.response_parser import extract_json
from src.rag.vector_store import VectorStore

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
) -> Reflection:
    """LLMで振り返りを生成する。"""
    elapsed_hours = (datetime.now() - previous_cycle_time).total_seconds() / 3600
    price_change = current_price - previous_entry_price
    price_change_pct = (price_change / previous_entry_price) * 100

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
        entry_id=f"ref_{pair_cfg.symbol}_{previous_cycle_time.strftime('%Y%m%d%H%M')}",
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
    )
    logger.info(f"[REFLECT] {reflection.pair}: stored to RAG | id={reflection.entry_id}")
