from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.analysis.rss_fetcher import FetchResult
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


# ── カテゴリ別プロンプト ──────────────────────────────────────────

_CATEGORY_LABELS = {
    "fx": "FX Market",
    "global": "Global Economy",
    "japan": "Japan / JPY",
}

_CATEGORY_FOCUS = {
    "fx": (
        "Focus on direct FX market developments: currency pair movements, "
        "central bank policies, interest rate expectations, and currency flow dynamics.\n"
        "Score: positive = risk-on / bullish risk currencies, negative = risk-off / defensive."
    ),
    "global": (
        "Focus on how global macroeconomic and geopolitical events affect "
        "risk appetite, safe-haven flows, and broad currency market direction.\n"
        "Score: positive = risk-on / positive economic outlook, negative = risk-off / uncertainty."
    ),
    "japan": (
        "Focus on Japan-specific factors: BOJ monetary policy, Japanese economic data, "
        "trade dynamics, and geopolitical risks affecting Japan.\n"
        "Score: positive = bullish JPY (stronger yen), negative = bearish JPY (weaker yen)."
    ),
}

# ── データクラス ────────────────────────────────────────────────

@dataclass
class NewsSentiment:
    pair: str
    sentiment_score: float  # -1.0 (bearish) to +1.0 (bullish)
    confidence: float
    key_themes: list[str] = field(default_factory=list)
    time_horizon: str = "medium"
    bullish_factors: list[str] = field(default_factory=list)
    bearish_factors: list[str] = field(default_factory=list)
    geopolitical_risks: list[str] = field(default_factory=list)
    japan_factors: list[str] = field(default_factory=list)
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    news_count: int = 0
    recent_count: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    fetched_at: datetime = field(default_factory=datetime.now)


# ── ヘルパー ────────────────────────────────────────────────────

def _neutral_sentiment(category: str, reason: str = "") -> NewsSentiment:
    return NewsSentiment(
        pair=category,
        sentiment_score=0.0,
        confidence=0.1,
        summary=reason or f"News analysis unavailable for {category}. Neutral stance assumed.",
    )


def _build_sentiment(
    category: str, data: dict,
    sources: list[str], fetch_result: FetchResult,
) -> NewsSentiment:
    sentiment = NewsSentiment(
        pair=category,
        sentiment_score=max(-1.0, min(1.0, _to_float(data.get("sentiment_score"), 0.0))),
        confidence=max(0.0, min(1.0, _to_float(data.get("confidence"), 0.5))),
        key_themes=data.get("key_themes", []),
        bullish_factors=data.get("bullish_factors", []),
        bearish_factors=data.get("bearish_factors", []),
        summary=data.get("summary", ""),
        sources=sources,
        news_count=fetch_result.news_count,
        recent_count=fetch_result.recent_count,
        feeds_ok=fetch_result.feeds_ok,
        feeds_failed=fetch_result.feeds_failed,
        fetched_at=datetime.now(),
    )
    label = _CATEGORY_LABELS.get(category, category)
    logger.info(
        f"[NEWS] {label}: score={sentiment.sentiment_score:+.2f} "
        f"conf={sentiment.confidence:.2f} | {sentiment.summary}"
    )
    return sentiment


# ── 公開関数 ────────────────────────────────────────────────────

async def analyze_category_sentiment(
    category: str,
    fetch_result: FetchResult,
    llm: LLMClient,
    temperature: float = 0.3,
    user_notes: str = "",
    trade_lessons: str = "",
) -> NewsSentiment:
    """カテゴリのニュースをLLMでセンチメント分析する。"""
    label = _CATEGORY_LABELS.get(category, category)

    if not fetch_result.items:
        logger.info(f"[NEWS] {label}: No items. Returning neutral.")
        return _neutral_sentiment(category, "No relevant news available.")

    # ソース一覧を生成
    unique_sources = sorted(set(item.source for item in fetch_result.items))

    user_context = f"=== User's Perspective ===\n{user_notes}" if user_notes else ""
    if user_notes:
        logger.info(f"[NEWS] {label}: user_notes injected ({len(user_notes)} chars)")

    news_text = fetch_result.format_for_llm()
    user_prompt = render_prompt(
        "news_user.j2",
        category_label=label,
        news_text=news_text,
        news_count=fetch_result.news_count,
        recent_count=fetch_result.recent_count,
        sources_list=", ".join(unique_sources),
        category_focus=_CATEGORY_FOCUS.get(category, ""),
        user_context=user_context,
        trade_lessons=trade_lessons,
    )
    messages = [
        {"role": "system", "content": load_prompt("news_system.txt")},
        {"role": "user", "content": user_prompt},
    ]

    try:
        logger.info(f"[NEWS] Calling LLM for {label}...")
        response_text = await llm.chat(messages, temperature=temperature)
        logger.debug(f"[NEWS] {label}: LLM response length: {len(response_text)} chars")

        data = extract_json(response_text)
        return _build_sentiment(
            category, data,
            sources=unique_sources, fetch_result=fetch_result,
        )

    except Exception as e:
        logger.warning(f"[NEWS] {label}: Analysis failed: {e}. Returning neutral sentiment.")
        return _neutral_sentiment(category, f"Analysis failed: {e}")
