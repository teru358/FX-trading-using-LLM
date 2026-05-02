"""ニュース記事 URL から本文を抽出する。

新規 RSS 記事のみ対象に並列で HTTP fetch + trafilatura で本文抽出する。
失敗時は None を返し、例外は呼び出し側に伝播させない (graceful degradation)。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from src.analysis.rss_fetcher import NewsItem

logger = logging.getLogger(__name__)


async def fetch_article_body(
    url: str,
    *,
    timeout_seconds: float,
    max_chars: int,
    user_agent: str,
) -> str | None:
    """1 URL から本文文字列を取得する。失敗時は None。

    HTTP error / timeout / trafilatura 抽出失敗 / 予期せぬ例外、すべて None で吸収する。
    呼び出し側 cycle を止めないことが優先。
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        body = trafilatura.extract(html, include_comments=False, no_fallback=False)
        if not body:
            return None
        return body[:max_chars]
    except Exception as e:
        logger.debug(
            f"[ARTICLE] fetch failed for {url}: {type(e).__name__}: {e}"
        )
        return None
