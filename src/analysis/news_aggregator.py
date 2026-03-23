from __future__ import annotations

import logging

from src.analysis.news_analyzer import NewsSentiment
from src.config import AppConfig, InstrumentConfig
from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


def aggregate_news_sentiment(
    pair_cfg: InstrumentConfig,
    store: VectorStore,
    config: AppConfig,
) -> NewsSentiment:
    """ペアに関連するカテゴリの直近ニュース分析を集約する。

    カテゴリ選択:
      - fx, global: 全ペア共通
      - japan: JPYペアのみ

    JPYペア（quote=JPY）の場合、japanカテゴリのスコアを反転する。
    （japan positive = bullish JPY = bearish XXX/JPY）
    """
    categories = pair_cfg.news_categories
    entries = store.get_recent_category_news(
        categories, lookback_hours=config.rag.news_lookback_hours,
    )

    if not entries:
        logger.debug(f"[AGGREGATE] {pair_cfg.display_name}: no category news in RAG")
        return NewsSentiment(
            pair=pair_cfg.symbol,
            sentiment_score=0.0,
            confidence=0.3,
            summary="No recent category news available.",
        )

    # カテゴリごとに最新の1件ずつ取得（同一カテゴリの古いエントリは無視）
    latest_by_cat: dict[str, dict] = {}
    for entry in entries:
        cat = entry["metadata"].get("category", "")
        if cat not in latest_by_cat:
            latest_by_cat[cat] = entry

    jpy_is_quote = pair_cfg.quote_currency == "JPY"

    scores = []
    confs = []
    all_themes: list[str] = []
    summaries: list[str] = []

    for cat, entry in latest_by_cat.items():
        meta = entry["metadata"]
        score = float(meta.get("sentiment_score", 0))
        conf = float(meta.get("confidence", 0.5))

        # JPYがquoteのペア: japanカテゴリのスコアを反転
        # japan positive (= bullish JPY) → bearish USD/JPY なので
        if cat == "japan" and jpy_is_quote:
            score = -score

        scores.append(score)
        confs.append(conf)

        themes_raw = meta.get("key_themes", "")
        all_themes.extend(t.strip() for t in themes_raw.split(",") if t.strip())
        summary = meta.get("summary", "")
        if summary:
            summaries.append(f"[{cat}] {summary}")

    avg_score = sum(scores) / len(scores)
    avg_conf = sum(confs) / len(confs)
    unique_themes = list(dict.fromkeys(all_themes))[:5]
    combined_summary = (
        f"Aggregated {len(latest_by_cat)} categories "
        f"({', '.join(latest_by_cat.keys())}): avg={avg_score:+.2f}"
    )

    logger.info(
        f"[AGGREGATE] {pair_cfg.display_name}: "
        f"categories={list(latest_by_cat.keys())} "
        f"score={avg_score:+.2f} conf={avg_conf:.2f}"
    )

    return NewsSentiment(
        pair=pair_cfg.symbol,
        sentiment_score=avg_score,
        confidence=min(avg_conf, 0.9),
        key_themes=unique_themes,
        summary=combined_summary,
    )
