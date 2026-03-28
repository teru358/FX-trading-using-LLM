from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pathlib import Path

from src.data.indicator_formatter import format_for_llm
from src.data.indicators import IndicatorSummary
from src.data.price_fetcher import PriceData
from src.llm.client import LLMClient
from src.llm.response_parser import extract_json

logger = logging.getLogger(__name__)

ASK_SYSTEM_PROMPT = """You are an expert FX swing trader and technical analyst with 20 years of experience.
The user will ask questions or share observations about the FX market.
Answer concisely and practically based on the provided analysis context.
Always respond in Japanese.
"""

ASK_USER_PROMPT_TEMPLATE = """{context}

=== User's Question / Comment ===
{user_message}

上記のコンテキストをもとに、簡潔かつ実践的に日本語で回答してください。"""


def _to_float(val, default: float) -> float:
    """LLMレスポンスの値を float に変換する。空文字・None・変換不能な場合はデフォルト値を返す。"""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _validate_sl_tp(analysis: "PriceAnalysis") -> str | None:
    """SL/TPが発注方向と矛盾していないか検証する。問題なければNone、矛盾があればエラーメッセージを返す。"""
    direction = analysis.direction_bias
    if direction == "neutral":
        return None
    entry_low, entry_high = analysis.entry_zone
    sl = analysis.stop_loss
    tp = analysis.take_profit
    if direction == "long":
        if sl >= entry_low:
            return f"LONG requires stop_loss < entry_zone[0]={entry_low:.5f}, got stop_loss={sl:.5f}"
        if tp <= entry_high:
            return f"LONG requires take_profit > entry_zone[1]={entry_high:.5f}, got take_profit={tp:.5f}"
    elif direction == "short":
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


SYSTEM_PROMPT = """You are an expert FX swing trader and technical analyst with 20 years of experience.
Analyze price data and technical indicators to produce a swing trading recommendation (3-10 day horizon).
Think step by step through the analysis before providing your final JSON output.
Be conservative: only recommend action when there is clear confluence of signals.
"""

USER_PROMPT_TEMPLATE = """{formatted_data}

{news_context}

{reflection_context}

{previous_analysis}

{macro_context}

{user_context}

Based on ALL of the above, provide your swing trading analysis for {pair}.

Consider:
1. Trend direction and strength (SMA alignment, ADX)
2. Momentum (RSI, MACD histogram direction)
3. Volatility and position within Bollinger Bands
4. Key support/resistance from recent swing highs/lows
5. Ichimoku Kinko Hyo: price vs Kumo, TK cross, Kumo color, overall Ichimoku signal
   - Price above Kumo = bullish bias; below = bearish; inside = consolidation
   - Bullish TK cross (Tenkan crosses above Kijun) = entry signal
   - Kumo acts as dynamic support/resistance zone for SL/TP placement
6. Chart Patterns (if detected): use as entry timing confirmation or early reversal warning
   - Bullish patterns (hammer, morning_star, bullish_engulfing, etc.) support long bias
   - Bearish patterns (shooting_star, evening_star, bearish_engulfing, etc.) support short bias
   - Neutral patterns (doji, inside_bar, bb_squeeze) suggest indecision — wait for confirmation
   - Require confluence with at least one other indicator before acting on patterns alone
7. News sentiment and macroeconomic context from the RAG knowledge base
8. Lessons learned from previous trading reflections
9. Compare with previous analysis: note any shift in direction or confidence since the last cycle
10. Macro context: equity index trends as risk sentiment indicators, cross-currency correlation
11. Risk/reward ratio (minimum 2:1 required)
    - For LONG:  stop_loss < entry_zone[0], take_profit > entry_zone[1]
    - For SHORT: stop_loss > entry_zone[1], take_profit < entry_zone[0]

After your reasoning, output ONLY this JSON block (no markdown fences):
{{
  "direction_bias": "long|short|neutral",
  "bias_score": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "entry_zone": [<low_price>, <high_price>],
  "stop_loss": <price>,   // long: below entry_zone[0] | short: above entry_zone[1]
  "take_profit": <price>, // long: above entry_zone[1] | short: below entry_zone[0]
  "risk_reward_ratio": <float>,
  "reasoning_summary": "<日本語1文：ニュース・振り返りを踏まえた分析根拠>"
}}"""


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

    user_prompt = USER_PROMPT_TEMPLATE.format(
        formatted_data=formatted_data,
        pair=pair_cfg.display_name,
        news_context=news_context or "=== News Context ===\nNo news data available yet.",
        reflection_context=reflection_context or "=== Reflections ===\nNo previous reflections yet.",
        previous_analysis=previous_analysis or "=== Previous Analysis ===\nNo previous analysis available.",
        macro_context=macro_context or "=== Macro Reference ===\nNo macro instrument data available.",
        user_context=user_context,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
            logger.error(f"Failed to parse Ollama JSON for {pair_cfg.display_name}: {e}")
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
    user_prompt = ASK_USER_PROMPT_TEMPLATE.format(
        context=context,
        user_message=user_message,
    )
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    logger.info(f"[ASK] LLM呼び出し中 ({len(user_message)} chars)...")
    response = await llm.chat(messages, temperature=temperature)
    # <think>ブロックを除去（deepseek-r1等のモデル対応）
    import re
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    return response
