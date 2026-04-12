from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

logger = logging.getLogger(__name__)

# フィードごとの最大取得件数（ソース多様性確保）
_MAX_PER_FEED = 5
# 全体の最大件数
_MAX_TOTAL = 30
# デフォルト鮮度フィルタ（時間）
_DEFAULT_FRESHNESS_HOURS = 24


# ── データクラス ────────────────────────────────────────────────

@dataclass
class NewsItem:
    """取得した個別ニュース記事。"""
    title: str
    summary: str
    source: str          # フィードURL（短縮名）
    published: datetime | None  # 発行日時（UTC）、取得不能時は None
    age_hours: float | None     # 取得時点での経過時間


@dataclass
class FetchResult:
    """RSS取得結果の詳細。"""
    items: list[NewsItem] = field(default_factory=list)
    total_feeds: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    recent_count: int = 0   # 発行日時が freshness_hours 以内の件数

    @property
    def news_count(self) -> int:
        return len(self.items)

    @property
    def newest_published_ts(self) -> float | None:
        """最新記事のUNIXタイムスタンプ（日時不明記事のみの場合は None）。"""
        timestamps = [item.published.timestamp() for item in self.items if item.published]
        return max(timestamps) if timestamps else None

    @property
    def articles_fingerprint(self) -> str:
        """記事タイトルセットのMD5ハッシュ（日時不明記事を含む新着検出用）。"""
        titles = sorted(item.title for item in self.items)
        return hashlib.md5("\n".join(titles).encode()).hexdigest()[:8]

    @property
    def title_hashes(self) -> frozenset[str]:
        """各記事タイトルの短縮ハッシュセット（新規/既知判定用）。"""
        return frozenset(
            hashlib.md5(item.title.encode()).hexdigest()[:8]
            for item in self.items
        )

    @property
    def title_hashes_csv(self) -> str:
        """タイトルハッシュのカンマ区切り文字列（メタデータ保存用）。"""
        return ",".join(sorted(self.title_hashes))

    def format_for_llm(self) -> str:
        """LLMプロンプト用にフォーマットする（日時情報付き）。"""
        if not self.items:
            return "No relevant news found in RSS feeds."
        lines = []
        for item in self.items:
            age_str = f"{item.age_hours:.1f}h ago" if item.age_hours is not None else "time unknown"
            lines.append(f"- [{age_str}] [{item.source}] {item.title}: {item.summary}")
        return "\n".join(lines)

    def format_titles_log(self) -> str:
        """ログ用：取得記事タイトル一覧。"""
        if not self.items:
            return "(no items)"
        lines = []
        for i, item in enumerate(self.items, 1):
            age_str = f"{item.age_hours:.1f}h" if item.age_hours is not None else "?h"
            lines.append(f"  {i:2d}. [{age_str}] [{item.source}] {item.title}")
        return "\n".join(lines)


# ── ヘルパー ────────────────────────────────────────────────────

def _parse_published(item) -> datetime | None:
    """feedparser エントリから UTC datetime を抽出する。"""
    # feedparser は published_parsed / updated_parsed を time.struct_time で提供
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(item, attr, None)
        if parsed is not None:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                return dt
            except (TypeError, ValueError):
                continue
    # フォールバック: published / updated 文字列を直接パース
    for attr in ("published", "updated"):
        raw = getattr(item, attr, None) or item.get(attr, "")
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def feed_short_name(url: str) -> str:
    """フィードURLから短縮名を生成する。"""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        # www. を除去
        if host.startswith("www."):
            host = host[4:]
        # ドメイン部分のみ（例: feeds.reuters.com → reuters）
        parts = host.split(".")
        if len(parts) >= 2:
            # feeds.feedburner.com → feedburner, www.fxstreet.com → fxstreet
            return parts[-2] if parts[-2] not in ("co", "com", "or", "ne") else parts[-3] if len(parts) > 2 else parts[0]
        return parts[0]
    except Exception:
        return url[:20]


# ── 共通フェッチロジック ──────────────────────────────────────────

def _fetch_from_feeds(
    feeds: list[str],
    keywords: frozenset[str] | None,
    freshness_hours: float,
    max_per_feed: int,
    max_total: int,
    summary_max_chars: int = 600,
) -> FetchResult:
    """フィードリストからニュースを取得する共通ロジック。

    keywords が None の場合、キーワードフィルタをスキップ（全記事取得）。
    """
    now = datetime.now(timezone.utc)
    result = FetchResult(total_feeds=len(feeds))
    all_items: list[NewsItem] = []
    seen: set[str] = set()

    for feed_url in feeds:
        feed_count = 0
        try:
            feed = feedparser.parse(feed_url)
            source_name = feed_short_name(feed_url)
            result.feeds_ok += 1

            for item in feed.entries[:20]:
                if feed_count >= max_per_feed:
                    break

                title = item.get("title", "").strip()
                body = item.get("summary", "")[:summary_max_chars].strip()
                if not title:
                    continue

                if title in seen:
                    continue

                # キーワードマッチ（None = フィルタなし）
                if keywords is not None:
                    combined = (title + " " + body).lower()
                    if not any(kw in combined for kw in keywords):
                        continue

                pub_dt = _parse_published(item)
                age_hours: float | None = None
                if pub_dt is not None:
                    age_hours = (now - pub_dt).total_seconds() / 3600
                    if age_hours > freshness_hours:
                        continue

                all_items.append(NewsItem(
                    title=title, summary=body, source=source_name,
                    published=pub_dt, age_hours=age_hours,
                ))
                seen.add(title)
                feed_count += 1

        except Exception as e:
            result.feeds_failed += 1
            logger.debug(f"RSS feed {feed_url} failed: {e}")

    # 新しい記事を優先: 日時ありを先頭に（新しい順）、日時なしを末尾に
    items_with_date = [i for i in all_items if i.published is not None]
    items_no_date = [i for i in all_items if i.published is None]
    items_with_date.sort(key=lambda x: x.published, reverse=True)
    result.items = (items_with_date + items_no_date)[:max_total]

    for item in result.items:
        if item.age_hours is not None:
            result.recent_count += 1

    return result


# ── 公開関数 ────────────────────────────────────────────────────

def fetch_category_news(
    feeds: list[str],
    keywords: frozenset[str] | None = None,
    freshness_hours: float = _DEFAULT_FRESHNESS_HOURS,
    max_per_feed: int = _MAX_PER_FEED,
    max_total: int = _MAX_TOTAL,
    summary_max_chars: int = 600,
) -> FetchResult:
    """カテゴリ単位でRSSフィードからニュースを取得する。

    keywords=None でキーワードフィルタを無効化（FX専門フィード向け）。
    """
    return _fetch_from_feeds(feeds, keywords, freshness_hours, max_per_feed, max_total, summary_max_chars)


