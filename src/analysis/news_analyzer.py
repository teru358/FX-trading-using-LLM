from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.analysis.rss_fetcher import fetch_rss_news
from src.config import NewsSourcesConfig
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

# ── プロンプトテンプレート ──────────────────────────────────

SYSTEM_PROMPT = """You are an expert FX analyst specializing in macroeconomic and geopolitical analysis.
Analyze news headlines to produce a sentiment assessment for swing trading (3-10 day horizon).
Be objective: weigh bullish and bearish factors carefully before assigning a score.
"""

RSS_PROMPT_TEMPLATE = """Below are recent news headlines and summaries relevant to {pair} ({base}/{quote}).
Sources include FX news, global business news, and Japan-specific news.

=== Recent News ===
{news_text}

=== Task ===
Based on the above, assess the {base}/{quote} swing trading outlook (3-10 day horizon).
Consider: central bank policy, economic data, geopolitical risks, Japan-specific factors (for JPY pairs), risk sentiment.

Return ONLY valid JSON (no markdown fences):
{{
  "sentiment_score": <float -1.0 to 1.0, positive means bullish {base}/{quote}>,
  "confidence": <float 0.0 to 1.0>,
  "time_horizon": "short|medium",
  "key_themes": ["theme1", "theme2"],
  "bullish_factors": ["factor1"],
  "bearish_factors": ["factor1"],
  "geopolitical_risks": ["risk1"],
  "japan_factors": ["factor1"],
  "summary": "<one sentence>"
}}"""


# ── データクラス ────────────────────────────────────────────

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
    fetched_at: datetime = field(default_factory=datetime.now)


# ── ヘルパー ────────────────────────────────────────────────

def _neutral_sentiment(symbol: str, pair: str, reason: str = "") -> NewsSentiment:
    return NewsSentiment(
        pair=symbol,
        sentiment_score=0.0,
        confidence=0.1,
        summary=reason or f"News analysis unavailable for {pair}. Neutral stance assumed.",
    )


def _build_sentiment(symbol: str, pair: str, data: dict, sources: list[str], news_count: int = 0) -> NewsSentiment:
    sentiment = NewsSentiment(
        pair=symbol,
        sentiment_score=max(-1.0, min(1.0, _to_float(data.get("sentiment_score"), 0.0))),
        confidence=max(0.0, min(1.0, _to_float(data.get("confidence"), 0.5))),
        key_themes=data.get("key_themes", []),
        time_horizon=data.get("time_horizon", "medium"),
        bullish_factors=data.get("bullish_factors", []),
        bearish_factors=data.get("bearish_factors", []),
        geopolitical_risks=data.get("geopolitical_risks", []),
        japan_factors=data.get("japan_factors", []),
        summary=data.get("summary", ""),
        sources=sources,
        news_count=news_count,
        fetched_at=datetime.now(),
    )
    logger.info(
        f"[NEWS] {pair}: score={sentiment.sentiment_score:+.2f} "
        f"conf={sentiment.confidence:.2f} | {sentiment.summary}"
    )
    return sentiment


# ── 公開関数 ────────────────────────────────────────────────

async def analyze_news_sentiment(
    pair_cfg,
    llm: LLMClient,
    temperature: float = 0.3,
    news_sources: NewsSourcesConfig | None = None,
) -> NewsSentiment:
    """RSSニュースを取得し、LLMでセンチメント分析する。"""
    pair = pair_cfg.display_name
    base = pair_cfg.base_currency
    quote = pair_cfg.quote_currency

    news_text, news_count = fetch_rss_news(base, quote, news_sources=news_sources)
    if not news_text or news_text.startswith("No relevant news"):
        logger.info(f"[NEWS] {pair}: No RSS news found. Returning neutral.")
        return _neutral_sentiment(pair_cfg.symbol, pair, "No relevant news available.")

    user_prompt = RSS_PROMPT_TEMPLATE.format(
        pair=pair, base=base, quote=quote, news_text=news_text,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        logger.info(f"[NEWS] Calling LLM for {pair}...")
        response_text = await llm.chat(messages, temperature=temperature)
        logger.debug(f"[NEWS] {pair}: LLM response length: {len(response_text)} chars")

        data = extract_json(response_text)
        return _build_sentiment(pair_cfg.symbol, pair, data, sources=["RSS"], news_count=news_count)

    except Exception as e:
        logger.warning(f"[NEWS] {pair}: Analysis failed: {e}. Returning neutral sentiment.")
        return _neutral_sentiment(pair_cfg.symbol, pair, f"Analysis failed: {e}")
