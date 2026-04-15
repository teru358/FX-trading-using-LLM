"""LLM による audit 改善ルール候補生成。

決済済みトレードの原分析 + 結果 + post_hoc データを LLM に渡し、
改善ルール候補 (rule_text / rationale / applicability) を JSON で受け取る。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.analysis.audit_post_hoc import (
    Counterfactuals,
    LessonCandidate,
    PostHocResult,
)
from src.analysis.prompt_loader import render_prompt
from src.llm.response_parser import extract_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "audit_lesson_system.txt"
)


def _load_system_prompt() -> str:
    if _SYSTEM_PROMPT_PATH.exists():
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    logger.warning(f"[AUDIT] System prompt not found: {_SYSTEM_PROMPT_PATH}")
    return "Propose lesson candidates as JSON."


async def generate_candidates(
    session: Any,
    post_hoc: PostHocResult,
    counterfactuals: Counterfactuals,
    vol_percentile: float | None,
    llm: Any,
    hint: str = "",
) -> list[LessonCandidate]:
    """LLM を呼んで改善ルール候補を生成する。

    失敗時 (LLM エラー / JSON パース失敗 / 必須フィールド欠損) は空リストを返す。
    """
    duration_h = post_hoc.duration_seconds / 3600

    try:
        user_prompt = render_prompt(
            "audit_lesson_user.j2",
            pair=session.pair,
            direction=session.direction,
            confidence=f"{session.signal_confidence:.2f}",
            analysis_summary=session.analysis_summary or "(none)",
            macro_context=session.macro_context or "(none)",
            realized_pnl=f"{session.realized_pnl:+.0f}",
            close_reason=session.close_reason or "-",
            duration=f"{duration_h:.1f}h",
            mfe_during=f"{post_hoc.mfe_during_trade:+.0f}",
            mae_during=f"{post_hoc.mae_during_trade:+.0f}",
            mfe_after=f"{post_hoc.mfe_after_close_24h:+.0f}",
            mae_after=f"{post_hoc.mae_after_close_24h:+.0f}",
            sl_cf=f"{counterfactuals.sl_minus_0_5_atr_pnl:+.0f}",
            tp_cf_0_5=f"{counterfactuals.tp_plus_0_5_atr_pnl:+.0f}",
            tp_cf_1_0=f"{counterfactuals.tp_plus_1_0_atr_pnl:+.0f}",
            vol_pct=f"{vol_percentile:.0f}" if vol_percentile is not None else "n/a",
            hint=hint,
        )
    except Exception as e:
        logger.warning(f"[AUDIT] Failed to render audit_lesson_user prompt: {e}")
        return []

    messages = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response_text = await llm.chat(messages, temperature=0.3)
    except Exception as e:
        logger.warning(f"[AUDIT] LLM candidate generation failed: {e}")
        return []

    try:
        parsed = extract_json(response_text)
    except Exception as e:
        logger.warning(
            f"[AUDIT] Could not parse LLM response: {e} | raw={response_text[:200]}"
        )
        return []

    if not isinstance(parsed, dict):
        logger.warning(f"[AUDIT] Parsed LLM response is not a dict: {parsed!r}")
        return []

    raw_candidates = parsed.get("candidates")
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[LessonCandidate] = []
    for c in raw_candidates[:5]:
        if not isinstance(c, dict):
            continue
        rule_text = c.get("rule_text")
        rationale = c.get("rationale")
        applicability = c.get("applicability")
        if not (rule_text and rationale and applicability):
            continue
        candidates.append(
            LessonCandidate(
                rule_text=str(rule_text),
                rationale=str(rationale),
                applicability=str(applicability),
                generated_at=datetime.now(),
                hint_used=hint,
            )
        )

    logger.info(
        f"[AUDIT] Generated {len(candidates)} candidates for {session.session_id}"
    )
    return candidates
