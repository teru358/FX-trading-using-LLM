"""Phase 4-2 検証: news_analysis が llama3.1-8b (llamacpp) で動作するか。

実際の finance 呼び出しパスを使って:
1. 短いモック RSS を組み立てる
2. llm = create_llm_client(config, "news_analysis") でクライアント生成
3. analyze_category_sentiment を実行
4. NewsSentiment が返ってきて JSON parse が通ることを確認
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.news_analyzer import analyze_category_sentiment
from src.analysis.rss_fetcher import FetchResult, NewsItem
from src.config import load_config
from src.llm.factory import create_llm_client


MOCK_ITEMS = [
    NewsItem(
        title="Fed Chair Powell signals more rate hikes if inflation persists",
        summary=(
            "Federal Reserve Chair Jerome Powell said the US central bank may raise "
            "interest rates further if inflation does not decline sustainably to 2%. "
            "Markets now price in 60% chance of a 25bp hike at the December meeting."
        ),
        source="Reuters",
        published=datetime.now(timezone.utc),
        age_hours=1.0,
    ),
    NewsItem(
        title="BOJ Governor Ueda hints at policy normalization in 2026",
        summary=(
            "Bank of Japan Governor Kazuo Ueda said conditions for ending negative "
            "interest rates are gradually being met. Market participants interpreted "
            "his comments as mildly hawkish, pushing yen stronger."
        ),
        source="Nikkei",
        published=datetime.now(timezone.utc),
        age_hours=2.0,
    ),
    NewsItem(
        title="US jobs data comes in stronger than expected",
        summary=(
            "Non-farm payrolls rose by 250,000 in October, exceeding forecasts of "
            "180,000. Unemployment fell to 3.8%. Wage growth accelerated. USD gained "
            "broadly against G10 currencies."
        ),
        source="Bloomberg",
        published=datetime.now(timezone.utc),
        age_hours=3.0,
    ),
]


async def main() -> None:
    config = load_config()
    print(f"Config: news_analysis provider={config.llm.news_analysis.provider!r}")
    print(f"        news_analysis model={config.llm.news_analysis.model!r}")

    llm = create_llm_client(config, "news_analysis")
    print(f"LLM client: {type(llm).__name__} model={llm.model_name}")

    fetch_result = FetchResult(
        items=MOCK_ITEMS,
        total_feeds=1,
        feeds_ok=1,
        feeds_failed=0,
        recent_count=len(MOCK_ITEMS),
    )

    print("\nCalling analyze_category_sentiment (fx category)...")
    print("NOTE: Initial call triggers llama-swap model load (~30-60s)")

    import time
    t0 = time.time()
    result = await analyze_category_sentiment(
        category="fx",
        fetch_result=fetch_result,
        llm=llm,
        temperature=config.llm.news_analysis.temperature,
    )
    elapsed = time.time() - t0

    print(f"\nResult (took {elapsed:.1f}s):")
    print(f"  sentiment_score: {result.sentiment_score:+.3f}")
    print(f"  confidence:      {result.confidence:.3f}")
    print(f"  key_themes:      {result.key_themes[:3]}")
    print(f"  summary:         {result.summary[:200]}")
    print(f"  news_count:      {result.news_count}")

    if result.summary == "No relevant news available." or result.confidence == 0.0:
        print("\n✗ Got neutral/fallback sentiment - LLM parse likely failed")
        sys.exit(1)

    print("\n✓ news_analysis succeeded with llamacpp")


if __name__ == "__main__":
    asyncio.run(main())
