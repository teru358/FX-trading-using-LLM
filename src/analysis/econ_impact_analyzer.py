"""LLMによる経済指標発表の影響分析。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.analysis.economic_calendar import EconEvent, classify_surprise
from src.analysis.prompt_loader import load_prompt, render_prompt
from src.llm.client import LLMClient

logger = logging.getLogger(__name__)

_IMPORTANCE_LABELS = {
    -1: "low",
    0: "medium",
    1: "high",
}


@dataclass
class PairReaction:
    """発表前後の価格反応データ。"""
    pair: str
    t_minus_5: float
    t_zero: float
    t_plus_5: float
    t_plus_15: float
    t_plus_30: float


@dataclass
class SnapshotBrief:
    pair: str
    bias_score: float
    confidence: float
    direction_bias: str


async def analyze_event_impact(
    event: EconEvent,
    pair_reactions: list[PairReaction],
    snapshots: list[SnapshotBrief],
    llm: LLMClient,
    temperature: float = 0.3,
) -> str:
    """指標発表の市場影響をLLMで分析する。"""
    surprise = classify_surprise(event.actual, event.forecast) or "unknown"
    user_prompt = render_prompt(
        "econ_impact_user.j2",
        event=event,
        importance_label=_IMPORTANCE_LABELS.get(event.importance, "unknown"),
        surprise_label=surprise,
        pair_reactions=pair_reactions,
        snapshots=snapshots,
    )
    messages = [
        {"role": "system", "content": load_prompt("econ_impact_system.txt")},
        {"role": "user", "content": user_prompt},
    ]
    logger.info(f"[ECON] Analyzing impact for {event.event_id}: {event.title[:40]}")
    response = await llm.chat(messages, temperature=temperature)
    # <think> タグ除去
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    return response
