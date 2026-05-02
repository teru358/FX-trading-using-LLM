"""news_collector の deep fetch 統合テスト。"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.rss_fetcher import FetchResult, NewsItem, title_hash
from src.config.schema import NewsCollectionConfig


def test_news_collection_config_has_deep_fetch_defaults():
    cfg = NewsCollectionConfig()
    assert cfg.deep_fetch_enabled is True
    assert cfg.deep_fetch_timeout_seconds == 8.0
    assert cfg.deep_fetch_max_chars == 3000
    assert cfg.deep_fetch_max_concurrent == 3
    assert cfg.deep_fetch_user_agent == "finance-news-collector/1.0"


def _make_item(title: str, link: str = "https://example.com/x") -> NewsItem:
    return NewsItem(
        title=title, summary="s", source="x",
        published=datetime.now(timezone.utc), age_hours=1.0,
        link=link,
    )


def _make_fake_config(deep_fetch_enabled: bool = True):
    """最小限の AppConfig like を作る。collect_category が触るフィールドだけ用意。"""
    cfg = MagicMock()
    cfg.news_collection.deep_fetch_enabled = deep_fetch_enabled
    cfg.news_collection.deep_fetch_timeout_seconds = 8.0
    cfg.news_collection.deep_fetch_max_chars = 3000
    cfg.news_collection.deep_fetch_max_concurrent = 3
    cfg.news_collection.deep_fetch_user_agent = "test/1.0"
    cfg.news_collection.news_freshness_hours = 24.0
    cfg.news_collection.summary_max_chars = 600
    cfg.news_sources.feedly.enabled = False
    cfg.news_sources.feedly.access_token = ""
    cfg.news_sources.feedly.streams_for = lambda c: []
    cfg.user_notes_path = None
    cfg.llm.news_analysis.temperature = 0.3
    cfg.rag.embedding_provider = "ollama"
    cfg.rag.embedding_base_url = "http://localhost:11434"
    cfg.rag.embedding_model = "nomic-embed-text"
    return cfg


@pytest.mark.asyncio
async def test_deep_fetch_only_called_for_new_items():
    """既知記事 (prev_hashes に含まれる) には deep fetch されず、新規だけが対象。"""
    from src.jobs import news_collector

    item_known = _make_item("known title", "https://example.com/known")
    item_new = _make_item("new title", "https://example.com/new")

    fetch_result = FetchResult(items=[item_known, item_new])
    fetch_result.feeds_ok = 1
    fetch_result.total_feeds = 1
    fetch_result.recent_count = 2

    prev_hashes = frozenset({title_hash("known title")})

    store = MagicMock()
    store.get_last_analysis_state = MagicMock(return_value=(None, "old_fp", prev_hashes))
    store.upsert_category_news = MagicMock()
    store.directional.query = MagicMock(return_value=[])

    cfg = _make_fake_config(deep_fetch_enabled=True)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value='{"sentiment_score": 0.0, "confidence": 0.5, "summary": "ok"}')

    fetched_links: list[str] = []

    async def _fake_fetch_bodies(items, **kwargs):
        for it in items:
            fetched_links.append(it.link)
            it.body = "fetched body"

    with patch.object(news_collector, "fetch_category_news", return_value=fetch_result), \
         patch.object(news_collector, "fetch_bodies_concurrent", side_effect=_fake_fetch_bodies), \
         patch.object(news_collector, "make_embed_fn", return_value=AsyncMock(return_value=[0.1, 0.2])), \
         patch.object(news_collector, "load_user_notes", return_value=""):
        await news_collector.collect_category(
            category="fx", feeds=["https://example.com/feed"], keywords=None,
            config=cfg, store=store, llm=llm,
        )

    # deep fetch は new title だけに走り、known title には走らない
    assert fetched_links == ["https://example.com/new"]
    # 既知記事は body=None のまま、新規記事に body 注入
    assert item_known.body is None
    assert item_new.body == "fetched body"


@pytest.mark.asyncio
async def test_deep_fetch_skipped_when_disabled():
    """deep_fetch_enabled=False で fetch_bodies_concurrent が呼ばれない。"""
    from src.jobs import news_collector

    item_new = _make_item("new title", "https://example.com/new")
    fetch_result = FetchResult(items=[item_new])
    fetch_result.feeds_ok = 1
    fetch_result.total_feeds = 1
    fetch_result.recent_count = 1

    store = MagicMock()
    store.get_last_analysis_state = MagicMock(return_value=(None, None, frozenset()))
    store.upsert_category_news = MagicMock()
    store.directional.query = MagicMock(return_value=[])

    cfg = _make_fake_config(deep_fetch_enabled=False)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value='{"sentiment_score": 0.0, "confidence": 0.5, "summary": "ok"}')

    fetch_bodies_mock = AsyncMock()

    with patch.object(news_collector, "fetch_category_news", return_value=fetch_result), \
         patch.object(news_collector, "fetch_bodies_concurrent", new=fetch_bodies_mock), \
         patch.object(news_collector, "make_embed_fn", return_value=AsyncMock(return_value=[0.1])), \
         patch.object(news_collector, "load_user_notes", return_value=""):
        await news_collector.collect_category(
            category="fx", feeds=["https://example.com/feed"], keywords=None,
            config=cfg, store=store, llm=llm,
        )

    fetch_bodies_mock.assert_not_called()
    assert item_new.body is None


@pytest.mark.asyncio
async def test_deep_fetch_skipped_when_fingerprint_unchanged():
    """記事セット fingerprint が前回と同じなら LLM/deep fetch とも走らず早期 return。"""
    from src.jobs import news_collector

    item = _make_item("same title", "https://example.com/x")
    fetch_result = FetchResult(items=[item])
    fetch_result.feeds_ok = 1
    fetch_result.total_feeds = 1
    fetch_result.recent_count = 1

    # current = prev → new_count=0
    prev_hashes = frozenset({title_hash("same title")})

    store = MagicMock()
    store.get_last_analysis_state = MagicMock(
        return_value=(None, fetch_result.articles_fingerprint, prev_hashes)
    )

    cfg = _make_fake_config(deep_fetch_enabled=True)
    llm = MagicMock()
    llm.chat = AsyncMock()

    fetch_bodies_mock = AsyncMock()

    with patch.object(news_collector, "fetch_category_news", return_value=fetch_result), \
         patch.object(news_collector, "fetch_bodies_concurrent", new=fetch_bodies_mock):
        await news_collector.collect_category(
            category="fx", feeds=["https://example.com/feed"], keywords=None,
            config=cfg, store=store, llm=llm,
        )

    fetch_bodies_mock.assert_not_called()
    llm.chat.assert_not_called()
