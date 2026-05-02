"""ニュース記事 URL から本文を抽出する。

新規 RSS 記事のみ対象に並列で HTTP fetch + trafilatura で本文抽出する。
失敗時は None を返し、例外は呼び出し側に伝播させない (graceful degradation)。

Cloudflare 等の bot 対策で UA だけでなく TLS フィンガープリントを検査するサイト
(investing.com 等) があるため、curl_cffi で Chrome を impersonate して TLS 層
から偽装する。httpx では HTTP 403 が返り本文取得に失敗していた。
"""
from __future__ import annotations

import asyncio
import logging

import trafilatura
from curl_cffi.requests import AsyncSession

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
        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={"User-Agent": user_agent},
                timeout=timeout_seconds,
                allow_redirects=True,
            )
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


async def fetch_bodies_concurrent(
    items: list[NewsItem],
    *,
    max_concurrent: int,
    timeout_seconds: float,
    max_chars: int,
    user_agent: str,
) -> None:
    """items の各 NewsItem.link を並列に fetch、結果を item.body に in-place で書く。

    link が空の item は skip。fetch_article_body は契約上例外を投げない (内部で
    全例外を吸収して None を返す) ため通常は inner try/except に入らないが、
    将来 fetch_article_body の契約が変わって例外が漏れた場合の defense-in-depth
    として残している。
    """
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(item: NewsItem) -> None:
        if not item.link:
            return
        async with sem:
            try:
                item.body = await fetch_article_body(
                    item.link,
                    timeout_seconds=timeout_seconds,
                    max_chars=max_chars,
                    user_agent=user_agent,
                )
            except Exception as e:
                logger.debug(
                    f"[ARTICLE] concurrent fetch failed for {item.link}: "
                    f"{type(e).__name__}: {e}"
                )
                item.body = None

    await asyncio.gather(*[_one(it) for it in items], return_exceptions=False)
