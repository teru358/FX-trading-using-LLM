"""news_collector の deep fetch 統合テスト。"""
from __future__ import annotations

from src.config.schema import NewsCollectionConfig


def test_news_collection_config_has_deep_fetch_defaults():
    cfg = NewsCollectionConfig()
    assert cfg.deep_fetch_enabled is True
    assert cfg.deep_fetch_timeout_seconds == 8.0
    assert cfg.deep_fetch_max_chars == 3000
    assert cfg.deep_fetch_max_concurrent == 3
    assert cfg.deep_fetch_user_agent == "finance-news-collector/1.0"
