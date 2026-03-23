"""Feedly API からニュースを取得するクライアント。

使い方:
  1. feedly.com/i/developer で Personal Access Token を取得
  2. .env に FEEDLY_ACCESS_TOKEN=<token> を設定（または settings.yaml に直接記載）
  3. settings.yaml の news_sources.feedly.streams_* にカテゴリの Stream ID を設定

Stream ID の確認方法:
  GET https://cloud.feedly.com/v3/categories
  Authorization: Bearer <token>
  → 各カテゴリの "id" フィールドが Stream ID（例: "user/xxx.../category/FX"）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from src.analysis.rss_fetcher import FetchResult, NewsItem

logger = logging.getLogger(__name__)

_FEEDLY_API_BASE = "https://cloud.feedly.com/v3"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text).strip()


def fetch_feedly_category(
    stream_ids: list[str],
    access_token: str,
    freshness_hours: float = 24.0,
    count: int = 20,
    summary_max_chars: int = 600,
) -> FetchResult:
    """Feedly API の複数ストリームからニュースを取得して FetchResult を返す。

    出力は rss_fetcher.fetch_category_news() と同じ型のため、
    下流の LLM 分析・RAG 格納ロジックは変更不要。
    """
    now = datetime.now(timezone.utc)
    newer_than_ms = int((now.timestamp() - freshness_hours * 3600) * 1000)

    result = FetchResult(total_feeds=len(stream_ids))
    all_items: list[NewsItem] = []
    seen: set[str] = set()
    headers = {"Authorization": f"Bearer {access_token}"}

    for stream_id in stream_ids:
        try:
            resp = httpx.get(
                f"{_FEEDLY_API_BASE}/streams/contents",
                params={"streamId": stream_id, "count": count, "newerThan": newer_than_ms},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            entries = resp.json().get("items", [])
            result.feeds_ok += 1

            for entry in entries:
                title = (entry.get("title") or "").strip()
                if not title or title in seen:
                    continue

                # summary または content フィールドから本文取得
                summary_obj = entry.get("summary") or entry.get("content") or {}
                raw_body = (summary_obj.get("content") or "")[:summary_max_chars * 2]
                body = _strip_html(raw_body)[:summary_max_chars]

                # published (ミリ秒 UNIX タイムスタンプ)
                pub_ms = entry.get("published")
                pub_dt: datetime | None = None
                age_hours: float | None = None
                if pub_ms:
                    pub_dt = datetime.fromtimestamp(pub_ms / 1000, tz=timezone.utc)
                    age_hours = (now - pub_dt).total_seconds() / 3600
                    if age_hours > freshness_hours:
                        continue

                source_name = (entry.get("origin") or {}).get("title") or stream_id.split("/")[-1]

                all_items.append(NewsItem(
                    title=title,
                    summary=body,
                    source=source_name,
                    published=pub_dt,
                    age_hours=age_hours,
                ))
                seen.add(title)

        except httpx.HTTPStatusError as e:
            result.feeds_failed += 1
            status = e.response.status_code
            if status == 401:
                logger.warning("Feedly: 認証失敗（access_token を確認してください）")
            else:
                logger.warning(f"Feedly stream {stream_id!r}: HTTP {status}")
        except Exception as e:
            result.feeds_failed += 1
            logger.debug(f"Feedly stream {stream_id!r} failed: {e}")

    # 新しい記事を優先（日時あり → 降順、日時なし → 末尾）
    items_with_date = sorted(
        [i for i in all_items if i.published is not None],
        key=lambda x: x.published,  # type: ignore[arg-type]
        reverse=True,
    )
    items_no_date = [i for i in all_items if i.published is None]
    result.items = items_with_date + items_no_date

    for item in result.items:
        if item.age_hours is not None:
            result.recent_count += 1

    return result
