"""発注時のコンテキストを網羅的にテキスト化する。"""
from __future__ import annotations
from src.trading.atr_calculator import SLTPResult

def build_entry_context(
    combined_score: float, confidence: float, action: str,
    news_weight: float, price_weight: float,
    news, price, sltp: SLTPResult, macro_context: str = "",
) -> str:
    sections = []
    sections.append(
        f"=== Signal Summary ===\n"
        f"combined_score={combined_score:+.3f} confidence={confidence:.2f} action={action}\n"
        f"news_weight={news_weight} price_weight={price_weight}"
    )
    themes = ", ".join(news.key_themes[:5]) if hasattr(news, "key_themes") and news.key_themes else "N/A"
    bullish = ", ".join(news.bullish_factors[:3]) if hasattr(news, "bullish_factors") and news.bullish_factors else "N/A"
    bearish = ", ".join(news.bearish_factors[:3]) if hasattr(news, "bearish_factors") and news.bearish_factors else "N/A"
    sections.append(
        f"=== News Sentiment ===\n"
        f"score={news.sentiment_score:+.2f} confidence={news.confidence:.2f}\n"
        f"key_themes: {themes}\nbullish_factors: {bullish}\nbearish_factors: {bearish}\n"
        f"summary: {getattr(news, 'summary', '')}"
    )
    entry_zone = getattr(price, "entry_zone", (0, 0))
    key_s = f" key_support={sltp.key_support:.5f}" if sltp.key_support else ""
    key_r = f" key_resistance={sltp.key_resistance:.5f}" if sltp.key_resistance else ""
    sections.append(
        f"=== Technical Analysis ===\n"
        f"direction={price.direction_bias} bias_score={price.bias_score:+.2f} confidence={price.confidence:.2f}\n"
        f"reasoning: {getattr(price, 'reasoning_summary', '')}\n"
        f"entry_zone=[{entry_zone[0]:.5f}, {entry_zone[1]:.5f}]{key_s}{key_r}"
    )
    sections.append(f"=== SL/TP Decision ===\n{sltp.comparison_text()}")
    if macro_context:
        sections.append(f"=== Macro Context ===\n{macro_context}")
    return "\n\n".join(sections)
