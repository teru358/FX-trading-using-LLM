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

if TYPE_CHECKING:
    from src.signals.technical_scorer import TechnicalScore

logger = logging.getLogger(__name__)

def _to_float(val, default: float) -> float:
    """LLMレスポンスの値を float に変換する。空文字・None・変換不能な場合はデフォルト値を返す。"""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# SL/TP が entry_zone からこの割合以内なら自動補正する閾値 (0.2%)
# これを超える大きな逸脱は LLM のロジックエラーとみなし補正しない
_AUTO_CORRECT_TOLERANCE_PCT = 0.002


def _validate_sl_tp(analysis: "PriceAnalysis") -> str | None:
    """SL/TPが発注方向と矛盾していないか検証する。

    entry_zone 境界からの逸脱が _AUTO_CORRECT_TOLERANCE_PCT 以内なら
    micro-margin (0.02%) で算術補正する。それを超える逸脱は LLM の
    ロジックエラーとしてエラーメッセージを返しリトライに回す。
    問題なければNone、補正しきれなければエラー文字列を返す。
    """
    direction = analysis.direction_bias
    if direction == "neutral":
        return None
    entry_low, entry_high = analysis.entry_zone
    sl = analysis.stop_loss
    tp = analysis.take_profit
    margin = entry_high * 0.0002  # 0.02% の補正マージン
    tolerance_sl = entry_low * _AUTO_CORRECT_TOLERANCE_PCT
    tolerance_tp = entry_high * _AUTO_CORRECT_TOLERANCE_PCT

    if direction == "long":
        # SL: entry_low 未満にあるべき
        if sl >= entry_low:
            if sl - entry_low <= tolerance_sl:
                corrected = entry_low - margin
                logger.warning(
                    f"[PRICE] SL auto-corrected for LONG: {sl:.5f} → {corrected:.5f} "
                    f"(was >= entry_zone[0]={entry_low:.5f})"
                )
                analysis.stop_loss = sl = corrected
            else:
                return f"LONG requires stop_loss < entry_zone[0]={entry_low:.5f}, got stop_loss={sl:.5f}"
        # TP: entry_high 超過にあるべき
        if tp <= entry_high:
            if entry_high - tp <= tolerance_tp:
                corrected = entry_high + margin
                logger.warning(
                    f"[PRICE] TP auto-corrected for LONG: {tp:.5f} → {corrected:.5f} "
                    f"(was <= entry_zone[1]={entry_high:.5f})"
                )
                analysis.take_profit = tp = corrected
            else:
                return f"LONG requires take_profit > entry_zone[1]={entry_high:.5f}, got take_profit={tp:.5f}"
    elif direction == "short":
        # SL: entry_high 超過にあるべき
        if sl <= entry_high:
            if entry_high - sl <= tolerance_sl:
                corrected = entry_high + margin
                logger.warning(
                    f"[PRICE] SL auto-corrected for SHORT: {sl:.5f} → {corrected:.5f} "
                    f"(was <= entry_zone[1]={entry_high:.5f})"
                )
                analysis.stop_loss = sl = corrected
            else:
                return f"SHORT requires stop_loss > entry_zone[1]={entry_high:.5f}, got stop_loss={sl:.5f}"
        # TP: entry_low 未満にあるべき
        if tp >= entry_low:
            if tp - entry_low <= tolerance_tp:
                corrected = entry_low - margin
                logger.warning(
                    f"[PRICE] TP auto-corrected for SHORT: {tp:.5f} → {corrected:.5f} "
                    f"(was >= entry_zone[0]={entry_low:.5f})"
                )
                analysis.take_profit = tp = corrected
            else:
                return f"SHORT requires take_profit < entry_zone[0]={entry_low:.5f}, got take_profit={tp:.5f}"
    return None


def _downgrade_to_neutral(analysis: "PriceAnalysis", reason: str) -> "PriceAnalysis":
    """SL/TP バリデーション全滅時に direction を neutral に降格させる。

    bias_score / trend_direction / support / resistance などのテクニカル情報は維持し、
    entry_zone / stop_loss / take_profit だけを current price 中心にクリアする。
    reasoning_summary に降格理由を追記する。
    """
    entry_low, entry_high = analysis.entry_zone
    mid = (entry_low + entry_high) / 2.0
    analysis.direction_bias = "neutral"
    analysis.entry_zone = (mid, mid)
    analysis.stop_loss = 0.0
    analysis.take_profit = 0.0
    analysis.risk_reward_ratio = 0.0
    suffix = f" [neutral降格: {reason}]"
    if suffix not in analysis.reasoning_summary:
        analysis.reasoning_summary = (analysis.reasoning_summary or "") + suffix
    return analysis


def _build_feedback(error: str, analysis: "PriceAnalysis") -> str:
    """バリデーション失敗時にLLMへ返すフィードバックメッセージを生成する。"""
    direction = analysis.direction_bias
    entry_low, entry_high = analysis.entry_zone
    if direction == "long":
        sl_rule = f"must be BELOW entry_zone[0] ({entry_low:.5f})"
        tp_rule = f"must be ABOVE entry_zone[1] ({entry_high:.5f})"
    else:
        sl_rule = f"must be ABOVE entry_zone[1] ({entry_high:.5f})"
        tp_rule = f"must be BELOW entry_zone[0] ({entry_low:.5f})"
    return (
        f"Your previous answer had an error in SL/TP values.\n\n"
        f"direction_bias: {direction}\n"
        f"entry_zone: [{entry_low:.5f}, {entry_high:.5f}]\n"
        f"stop_loss: {analysis.stop_loss:.5f}  <- {sl_rule}\n"
        f"take_profit: {analysis.take_profit:.5f}  <- {tp_rule}\n\n"
        f"Error: {error}\n\n"
        f"Please correct stop_loss and take_profit and output the full JSON again."
    )


@dataclass
class PriceAnalysis:
    pair: str
    direction_bias: str  # "long" | "short" | "neutral"
    bias_score: float  # -1.0 to +1.0
    confidence: float  # 0.0 to 1.0
    entry_zone: tuple[float, float]
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    reasoning_summary: str
    analyzed_at: datetime
    key_support: float | None = None
    key_resistance: float | None = None


def load_user_notes(notes_path: Path, section: str = "price") -> str:
    """user_notes.md の指定セクション (price/news/reflect) のテキストを返す。

    ## price / ## news / ## reflect の見出しでセクションを分割する。
    見出しがない場合はファイル全体を "price" として扱う（後方互換）。
    空または有効テキストなしの場合は空文字を返す。
    """
    if not notes_path.exists():
        return ""
    text = notes_path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    def _clean(raw: str) -> str:
        lines = [l for l in raw.splitlines() if l.strip() and not l.strip().startswith("#") and l.strip() != "---"]
        return "\n".join(lines).strip()

    parts = re.split(r"^##\s+(price|news|reflect)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if len(parts) == 1:
        # セクション見出しなし → 全体を "price" 扱い（後方互換）
        return _clean(parts[0]) if section.lower() == "price" else ""

    for i in range(1, len(parts), 2):
        if parts[i].lower() == section.lower():
            return _clean(parts[i + 1] if i + 1 < len(parts) else "")
    return ""


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
) -> PriceAnalysis:
    formatted_data = format_for_llm(price_data, summary)

    user_notes = load_user_notes(user_notes_path) if user_notes_path else ""
    if user_notes:
        user_context = f"=== User's Perspective ===\n{user_notes}"
        logger.info(f"[PRICE] {pair_cfg.display_name}: user_notes injected ({len(user_notes)} chars)")
    else:
        user_context = ""

    tech_score_context = ""
    if tech_score is not None:
        tech_score_context = tech_score.format_for_prompt()

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

        direction = data.get("direction_bias", "neutral")
        bias_score = _to_float(data.get("bias_score"), 0.0)
        confidence = _to_float(data.get("confidence"), 0.5)
        entry_zone_raw = data.get("entry_zone", [summary.current_price, summary.current_price])
        if not isinstance(entry_zone_raw, (list, tuple)) or len(entry_zone_raw) < 2:
            entry_zone_raw = [summary.current_price, summary.current_price]
        if direction == "short":
            stop_loss = _to_float(data.get("stop_loss"), summary.current_price * 1.01)
            take_profit = _to_float(data.get("take_profit"), summary.current_price * 0.98)
        else:
            stop_loss = _to_float(data.get("stop_loss"), summary.current_price * 0.99)
            take_profit = _to_float(data.get("take_profit"), summary.current_price * 1.02)
        rr = _to_float(data.get("risk_reward_ratio"), 2.0)
        key_support = _to_float(data.get("key_support"), 0.0) or None
        key_resistance = _to_float(data.get("key_resistance"), 0.0) or None

        analysis = PriceAnalysis(
            pair=pair_cfg.symbol,
            direction_bias=direction,
            bias_score=max(-1.0, min(1.0, bias_score)),
            confidence=max(0.0, min(1.0, confidence)),
            entry_zone=(
                _to_float(entry_zone_raw[0], summary.current_price),
                _to_float(entry_zone_raw[1], summary.current_price),
            ),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=rr,
            reasoning_summary=data.get("reasoning_summary", ""),
            analyzed_at=datetime.now(),
            key_support=key_support,
            key_resistance=key_resistance,
        )

        logger.info(
            f"[PRICE] {pair_cfg.display_name}: {direction} bias={bias_score:+.2f} "
            f"conf={confidence:.2f} RR={rr:.1f}"
        )

        if tech_score is not None:
            llm_bias = analysis.bias_score
            analysis.bias_score = tech_score.total_score
            analysis.confidence = tech_score.confidence
            analysis.direction_bias = tech_score.direction
            logger.info(
                f"[PRICE] {pair_cfg.display_name}: bias overridden "
                f"LLM={llm_bias:+.2f} → Rule={tech_score.total_score:+.2f}"
            )

        validation_error = _validate_sl_tp(analysis)
        if validation_error is None:
            return analysis

        last_error = validation_error
        logger.warning(
            f"[PRICE] {pair_cfg.display_name}: SL/TP validation failed (attempt {attempt + 1}): {validation_error}"
        )
        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": _build_feedback(validation_error, analysis)})

    logger.error(
        f"[PRICE] {pair_cfg.display_name}: SL/TP validation failed after {MAX_RETRIES} attempts. "
        f"Downgrading to neutral. Last error: {last_error}"
    )
    return _downgrade_to_neutral(analysis, last_error or "unknown validation error")


async def chat_with_context(
    user_message: str,
    context: str,
    llm: LLMClient,
    temperature: float = 0.3,
) -> str:
    """ユーザーのコメント・質問にコンテキスト付きで自然言語回答する。"""
    user_prompt = render_prompt(
        "ask_user.j2",
        context=context,
        user_message=user_message,
    )
    messages = [
        {"role": "system", "content": load_prompt("ask_system.txt")},
        {"role": "user", "content": user_prompt},
    ]
    logger.info(f"[ASK] LLM呼び出し中 ({len(user_message)} chars)...")
    response = await llm.chat(messages, temperature=temperature)
    # <think>ブロックを除去（deepseek-r1等のモデル対応）
    import re
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    return response
