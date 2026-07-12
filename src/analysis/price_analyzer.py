from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pathlib import Path
from typing import TYPE_CHECKING

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.data.indicator_formatter import format_for_llm
from src.data.indicators import IndicatorSummary
from src.data.price_fetcher import PriceData
from src.llm.client import LLMClient
from src.llm.response_parser import extract_json
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.signals.technical_scorer import MultiTfTechnicalScore, TechnicalScore

logger = logging.getLogger(__name__)

def _to_float(val, default: float) -> float:
    """LLMレスポンスの値を float に変換する。空文字・None・変換不能な場合はデフォルト値を返す。"""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@dataclass
class PriceAnalysis:
    pair: str
    direction_bias: str  # "long" | "short" | "neutral"
    bias_score: float  # -1.0 to +1.0
    confidence: float  # 0.0 to 1.0
    entry_zone: tuple[float, float]
    reasoning_summary: str
    analyzed_at: datetime
    key_support: float | None = None
    key_resistance: float | None = None
    market_regime: str = "unknown"       # "trending" | "ranging" | "breakout_pending" | "unknown"
    confidence_modifier: float = 0.0     # LLM による tech_score confidence の補正 (-0.1〜+0.1)
    # 後方互換: 旧LLM出力・snapshot aggが stop_loss/take_profit を含む場合に受容
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_reward_ratio: float = 0.0


def load_user_notes(notes_path: Path, section: str = "price") -> str:
    """user_notes.md の指定セクション (price/news/reflect/plan) のテキストを返す。

    ## price / ## news / ## reflect / ## plan の見出しでセクションを分割する。
    見出しがない場合はファイル全体を "price" として扱う（後方互換）。
    空または有効テキストなしの場合は空文字を返す。
    """
    if not notes_path.exists():
        return ""
    try:
        text = notes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"user_notes read failed ({notes_path}): {type(e).__name__}: {e}")
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    def _clean(raw: str) -> str:
        lines = [l for l in raw.splitlines() if l.strip() and not l.strip().startswith("#") and l.strip() != "---"]
        return "\n".join(lines).strip()

    parts = re.split(r"^##\s+(price|news|reflect|plan)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if len(parts) == 1:
        # セクション見出しなし → 全体を "price" 扱い（後方互換）
        return _clean(parts[0]) if section.lower() == "price" else ""

    for i in range(1, len(parts), 2):
        if parts[i].lower() == section.lower():
            return _clean(parts[i + 1] if i + 1 < len(parts) else "")
    return ""


def load_audit_lessons(path: Path) -> str:
    """audit_lessons.md から承認済みルールのテキストを返す。

    ファイル本体に `[LLM-PROPOSED / USER-APPROVED]` タグを含む見出しがない場合は
    空文字を返す (ヘッダーのみで意味のある教訓ゼロなら注入しない)。
    """
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    if "[LLM-PROPOSED / USER-APPROVED]" not in content:
        return ""
    return content


async def analyze_price_action(
    pair_cfg,
    price_data: PriceData,
    summary: IndicatorSummary,
    llm: LLMClient,
    temperature: float = 0.1,
    news_context: str = "",
    reflection_context: str = "",
    previous_analysis: str = "",
    macro_context: str = "",
    user_notes_path: Path | None = None,
    tech_score: "TechnicalScore | None" = None,
    mtf_score: "MultiTfTechnicalScore | None" = None,
) -> PriceAnalysis:
    formatted_data = format_for_llm(price_data, summary)

    user_notes = load_user_notes(user_notes_path) if user_notes_path else ""
    if user_notes:
        user_context = f"=== User's Perspective ===\n{user_notes}"
        logger.info(f"[PRICE] {pair_cfg.display_name}: user_notes injected ({len(user_notes)} chars)")
    else:
        user_context = ""

    # Audit lessons 注入 (Learned rules from past audits)
    audit_lessons_path = (
        user_notes_path.parent / "audit_lessons.md"
        if user_notes_path is not None else None
    )
    audit_lessons_text = load_audit_lessons(audit_lessons_path) if audit_lessons_path else ""
    if audit_lessons_text:
        audit_lessons_section = (
            "\n\n=== Learned Rules from Past Audits ===\n"
            "以下は過去取引から導出され、人間が承認した取引改善ルールです。\n"
            "該当条件をチェックし、取引判断に適用してください。\n\n"
            + audit_lessons_text
        )
        user_context = (user_context + audit_lessons_section) if user_context else audit_lessons_section.lstrip()
        logger.info(f"[PRICE] {pair_cfg.display_name}: audit_lessons injected ({len(audit_lessons_text)} chars)")

    tech_score_context = ""
    if tech_score is not None:
        tech_score_context = tech_score.format_for_prompt()

    mtf_score_context = ""
    if mtf_score is not None and mtf_score.tf_scores:
        mtf_score_context = (
            "=== Multi-Timeframe Alignment (rule-based, treat as constraint) ===\n"
            f"{mtf_score.format_for_prompt()}\n"
            "Action: 方向性と confidence は MTF 合成結果を踏襲し、短期 TF 情報を元に\n"
            "       具体的な entry_zone / stop_loss / take_profit / reasoning_summary を決定してください。"
        )

    user_prompt = render_prompt(
        "price_user.j2",
        formatted_data=formatted_data,
        pair=pair_cfg.display_name,
        news_context=news_context or "=== News Context ===\nNo news data available yet.",
        reflection_context=reflection_context or "=== Reflections ===\nNo previous reflections yet.",
        previous_analysis=previous_analysis or "=== Previous Analysis ===\nNo previous analysis available.",
        macro_context=macro_context or "=== Macro Reference ===\nNo macro instrument data available.",
        user_context=user_context,
        tech_score_context=tech_score_context,
        mtf_score_context=mtf_score_context,
    )

    messages = [
        {"role": "system", "content": load_prompt("price_system.txt")},
        {"role": "user", "content": user_prompt},
    ]

    MAX_RETRIES = 2
    last_error: str | None = None

    for attempt in range(MAX_RETRIES):
        logger.info(f"Calling LLM for {pair_cfg.display_name}... (attempt {attempt + 1}/{MAX_RETRIES})")
        response_text = await llm.chat(messages, temperature=temperature)
        logger.debug(f"LLM response length: {len(response_text)} chars")

        try:
            data = extract_json(response_text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = str(e)
            logger.warning(
                f"[PRICE] {pair_cfg.display_name}: JSON parse failed (attempt {attempt + 1}): {e}"
            )
            logger.debug(f"[PRICE] Raw response: {response_text[:500]}")
            if attempt < MAX_RETRIES - 1:
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "user", "content":
                    "Your response was not valid JSON. Please return ONLY a valid JSON object with no extra text."
                })
                continue
            raise

        # 新フォーマット (5フィールド) を優先、旧フォーマット (10フィールド) も後方互換で受容
        entry_zone_raw = data.get("entry_zone", [summary.current_price, summary.current_price])
        if not isinstance(entry_zone_raw, (list, tuple)) or len(entry_zone_raw) < 2:
            entry_zone_raw = [summary.current_price, summary.current_price]
        key_support = _to_float(data.get("key_support"), 0.0) or None
        key_resistance = _to_float(data.get("key_resistance"), 0.0) or None
        market_regime = data.get("market_regime", "unknown")
        if market_regime not in ("trending", "ranging", "breakout_pending"):
            market_regime = "unknown"
        confidence_modifier = max(-0.1, min(0.1, _to_float(data.get("confidence_modifier"), 0.0)))

        # 後方互換: 旧フォーマットのフィールドも受容
        direction = data.get("direction_bias", "neutral")
        bias_score = _to_float(data.get("bias_score"), 0.0)
        confidence = _to_float(data.get("confidence"), 0.5)
        stop_loss = _to_float(data.get("stop_loss"), 0.0)
        take_profit = _to_float(data.get("take_profit"), 0.0)
        rr = _to_float(data.get("risk_reward_ratio"), 0.0)

        analysis = PriceAnalysis(
            pair=pair_cfg.symbol,
            direction_bias=direction,
            bias_score=max(-1.0, min(1.0, bias_score)),
            confidence=max(0.0, min(1.0, confidence)),
            entry_zone=(
                _to_float(entry_zone_raw[0], summary.current_price),
                _to_float(entry_zone_raw[1], summary.current_price),
            ),
            reasoning_summary=data.get("reasoning_summary", ""),
            analyzed_at=db_now(),
            key_support=key_support,
            key_resistance=key_resistance,
            market_regime=market_regime,
            confidence_modifier=confidence_modifier,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=rr,
        )

        # tech_score override (direction/bias/confidence はルールベースが決定)
        if tech_score is not None:
            analysis.bias_score = tech_score.total_score
            analysis.direction_bias = tech_score.direction
            # confidence_modifier を tech_score.confidence に加算
            base_conf = tech_score.confidence
            analysis.confidence = max(0.0, min(1.0, base_conf + confidence_modifier))
            if confidence_modifier != 0.0:
                logger.info(
                    f"[PRICE] {pair_cfg.display_name}: conf={base_conf:.2f}{confidence_modifier:+.2f}"
                    f"={analysis.confidence:.2f} regime={market_regime}"
                )
            else:
                logger.info(
                    f"[PRICE] {pair_cfg.display_name}: conf={base_conf:.2f} regime={market_regime}"
                )
        else:
            logger.info(
                f"[PRICE] {pair_cfg.display_name}: {direction} bias={bias_score:+.2f} "
                f"conf={confidence:.2f} regime={market_regime}"
            )

        return analysis

    # ここに到達するのは全リトライで JSON パース失敗した場合のみ
    raise RuntimeError(
        f"[PRICE] {pair_cfg.display_name}: LLM analysis failed after {MAX_RETRIES} attempts"
    )
