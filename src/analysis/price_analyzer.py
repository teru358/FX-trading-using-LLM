from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pathlib import Path

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.data.indicator_formatter import format_for_llm
from src.data.indicators import IndicatorSummary
from src.data.price_fetcher import PriceData
from src.llm.client import LLMClient
from src.llm.response_parser import extract_json

logger = logging.getLogger(__name__)

def _to_float(val, default: float) -> float:
    """LLMレスポンスの値を float に変換する。空文字・None・変換不能な場合はデフォルト値を返す。"""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _validate_sl_tp(analysis: "PriceAnalysis") -> str | None:
    """SL/TPが発注方向と矛盾していないか検証する。
    同値の場合は微小マージン（価格の0.01%）を自動付与して補正する。
    問題なければNone、矛盾があればエラーメッセージを返す。
    """
    direction = analysis.direction_bias
    if direction == "neutral":
        return None
    entry_low, entry_high = analysis.entry_zone
    sl = analysis.stop_loss
    tp = analysis.take_profit
    margin = entry_high * 0.0001  # 0.01% の微小マージン
    if direction == "long":
        if sl == entry_low:
            analysis.stop_loss = sl = entry_low - margin
            logger.debug(f"SL auto-adjusted: {sl:.5f} (margin applied for LONG)")
        if tp == entry_high:
            analysis.take_profit = tp = entry_high + margin
            logger.debug(f"TP auto-adjusted: {tp:.5f} (margin applied for LONG)")
        if sl >= entry_low:
            return f"LONG requires stop_loss < entry_zone[0]={entry_low:.5f}, got stop_loss={sl:.5f}"
        if tp <= entry_high:
            return f"LONG requires take_profit > entry_zone[1]={entry_high:.5f}, got take_profit={tp:.5f}"
    elif direction == "short":
        if sl == entry_high:
            analysis.stop_loss = sl = entry_high + margin
            logger.debug(f"SL auto-adjusted: {sl:.5f} (margin applied for SHORT)")
        if tp == entry_low:
            analysis.take_profit = tp = entry_low - margin
            logger.debug(f"TP auto-adjusted: {tp:.5f} (margin applied for SHORT)")
        if sl <= entry_high:
            return f"SHORT requires stop_loss > entry_zone[1]={entry_high:.5f}, got stop_loss={sl:.5f}"
        if tp >= entry_low:
            return f"SHORT requires take_profit < entry_zone[0]={entry_low:.5f}, got take_profit={tp:.5f}"
    return None


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
) -> PriceAnalysis:
    formatted_data = format_for_llm(price_data, summary)

    user_notes = load_user_notes(user_notes_path) if user_notes_path else ""
    if user_notes:
        user_context = f"=== User's Perspective ===\n{user_notes}"
        logger.info(f"[PRICE] {pair_cfg.display_name}: user_notes injected ({len(user_notes)} chars)")
    else:
        user_context = ""

    user_prompt = render_prompt(
        "price_user.j2",
        formatted_data=formatted_data,
        pair=pair_cfg.display_name,
        news_context=news_context or "=== News Context ===\nNo news data available yet.",
        reflection_context=reflection_context or "=== Reflections ===\nNo previous reflections yet.",
        previous_analysis=previous_analysis or "=== Previous Analysis ===\nNo previous analysis available.",
        macro_context=macro_context or "=== Macro Reference ===\nNo macro instrument data available.",
        user_context=user_context,
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
        )

        logger.info(
            f"[PRICE] {pair_cfg.display_name}: {direction} bias={bias_score:+.2f} "
            f"conf={confidence:.2f} RR={rr:.1f}"
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

    raise ValueError(
        f"[PRICE] {pair_cfg.display_name}: SL/TP validation failed after {MAX_RETRIES} attempts. Last error: {last_error}"
    )


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
