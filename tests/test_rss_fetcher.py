"""rss_fetcher.NewsItem / FetchResult / _title_hash の単体テスト。"""
from __future__ import annotations

from datetime import datetime, timezone

from src.analysis.rss_fetcher import FetchResult, NewsItem, title_hash


def test_news_item_has_link_field_default_empty():
    item = NewsItem(title="t", summary="s", source="x", published=None, age_hours=None)
    assert item.link == ""


def test_news_item_has_body_field_default_none():
    item = NewsItem(title="t", summary="s", source="x", published=None, age_hours=None)
    assert item.body is None


def test_news_item_link_can_be_set():
    item = NewsItem(
        title="t", summary="s", source="x", published=None, age_hours=None,
        link="https://example.com/article",
    )
    assert item.link == "https://example.com/article"


def test_news_item_body_can_be_set():
    item = NewsItem(
        title="t", summary="s", source="x", published=None, age_hours=None,
        body="Full body text",
    )
    assert item.body == "Full body text"


def test_title_hash_is_md5_first_8_hex():
    # NewsItem.title から FetchResult.title_hashes と同じ規則でハッシュを得られる
    h = title_hash("Hello World")
    assert isinstance(h, str)
    assert len(h) == 8
    # 同一入力で同一出力 (deterministic)
    assert h == title_hash("Hello World")


def test_title_hash_matches_fetch_result_title_hashes():
    item = NewsItem(title="abc", summary="", source="x", published=None, age_hours=None)
    result = FetchResult(items=[item])
    assert title_hash("abc") in result.title_hashes
