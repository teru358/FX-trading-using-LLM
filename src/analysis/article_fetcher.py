"""ニュース記事 URL から本文を抽出する。

新規 RSS 記事のみ対象に並列で HTTP fetch + trafilatura で本文抽出する。
失敗時は None を返し、例外は呼び出し側に伝播させない (graceful degradation)。

Cloudflare 等の bot 対策で UA だけでなく TLS フィンガープリントを検査するサイト
(investing.com 等) があるため、curl_cffi で Chrome を impersonate して TLS 層
から偽装する。httpx では HTTP 403 が返り本文取得に失敗していた。

Google News RSS 経由のリンク (news.google.com/rss/articles/...) は wrapper URL で、
curl_cffi の redirect 追従で publisher に到達できれば抽出成功。Google の consent
ページに閉じ込められた場合は trafilatura に投げず早期 None で抜ける (ノイズ抑制)。
"""
from __future__ import annotations

import asyncio
import logging

import trafilatura
from curl_cffi.requests import AsyncSession

from src.analysis.rss_fetcher import NewsItem

logger = logging.getLogger(__name__)

# trafilatura は抽出失敗ごとに "discarding data: None" を WARNING で吐く。
# paywall や JS-only ページでは normal な失敗のため、ノイズとして抑制する。
# 本文取得の成否は news_collector の "deep fetch X/Y succeeded" ログで追跡する。
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)


def _is_google_news_url(url: str | None) -> bool:
    """URL が news.google.com の wrapper かどうか。"""
    if not url:
        return False
    return "://news.google.com/" in url


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
    is_google = _is_google_news_url(url)
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

            # Google News wrapper の redirect 追従後、最終 URL がまだ news.google.com
            # に閉じている (= consent ページ等で publisher に到達できていない) 場合は
            # trafilatura で抽出しても "discarding data" になるだけなので早期 None。
            if is_google:
                final_url = str(getattr(resp, "url", "") or "")
                if not final_url or _is_google_news_url(final_url):
                    logger.debug(
                        f"[ARTICLE] google news wrapper not resolved: "
                        f"{url} → {final_url or '<no final url>'}"
                    )
                    return None

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
