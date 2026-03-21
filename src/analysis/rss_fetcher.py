from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import feedparser

if TYPE_CHECKING:
    from src.config import NewsSourcesConfig

logger = logging.getLogger(__name__)

# ── デフォルトフィードリスト（settings.yaml 未設定時のフォールバック） ──

_DEFAULT_FEEDS_FX = [
    "https://feeds.feedburner.com/forexlive/all",
    "https://www.fxstreet.com/rss/news",
    "https://www.investing.com/rss/news_285.rss",
]

_DEFAULT_FEEDS_GLOBAL = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.apnews.com/rss/business",
    "https://www.ft.com/rss/home/uk",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.investing.com/rss/news_14.rss",
]

_DEFAULT_FEEDS_JAPAN = [
    "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml",
    "https://japantoday.com/feed",
    "https://www.japantimes.co.jp/feed/",
    "https://asia.nikkei.com/rss/feed/nar",
    "https://www.nikkei.com/news/category/?rss=true&bn=20",
]

_DEFAULT_JPY_KEYWORDS = frozenset({
    # 英語
    "boj", "bank of japan", "nippon ginko", "japan", "japanese",
    "yen", "jpy", "ueda",
    # 日本語
    "日銀", "円安", "円高", "金利", "為替", "利上げ", "利下げ", "植田", "円",
})


# ── 公開関数 ────────────────────────────────────────────────

def build_feed_list(
    base: str,
    quote: str,
    feeds_fx: list[str] | None = None,
    feeds_global: list[str] | None = None,
    feeds_japan: list[str] | None = None,
) -> list[str]:
    """通貨ペアに応じたRSSフィードリストを組み立てる。"""
    fx = feeds_fx if feeds_fx is not None else _DEFAULT_FEEDS_FX
    gl = feeds_global if feeds_global is not None else _DEFAULT_FEEDS_GLOBAL
    jp = feeds_japan if feeds_japan is not None else _DEFAULT_FEEDS_JAPAN
    feeds = list(fx) + list(gl)
    if {base.lower(), quote.lower()} & {"jpy", "yen", "japan"}:
        feeds += jp
    return feeds


def fetch_rss_news(
    base: str,
    quote: str,
    news_sources: NewsSourcesConfig | None = None,
) -> tuple[str, int]:
    """RSSフィードからFX関連ニュースを取得してテキストにまとめる。"""
    if news_sources is not None:
        feeds_fx = news_sources.feeds_fx
        feeds_global = news_sources.feeds_global
        feeds_japan = news_sources.feeds_japan
        jpy_keywords = frozenset(kw.lower() for kw in news_sources.jpy_keywords)
    else:
        feeds_fx = feeds_global = feeds_japan = None
        jpy_keywords = _DEFAULT_JPY_KEYWORDS

    base_keywords = {base.lower(), quote.lower(), "forex", "fx", "currency"}
    extra = jpy_keywords if {base.lower(), quote.lower()} & {"jpy"} else set()
    keywords = base_keywords | extra | {"central bank", "economy", "geopolit", "inflation", "rate"}

    feeds = build_feed_list(base, quote, feeds_fx, feeds_global, feeds_japan)
    entries: list[str] = []
    seen: set[str] = set()

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for item in feed.entries[:20]:
                title = item.get("title", "")
                body = item.get("summary", "")[:250]
                combined = (title + " " + body).lower()
                if title in seen:
                    continue
                if any(kw in combined for kw in keywords):
                    entries.append(f"- {title}: {body}")
                    seen.add(title)
                if len(entries) >= 20:
                    break
        except Exception as e:
            logger.debug(f"RSS feed {feed_url} failed: {e}")
        if len(entries) >= 20:
            break

    text = "\n".join(entries) if entries else "No relevant news found in RSS feeds."
    return text, len(entries)
