import math

import pytest

from src.config.schema import OrchestratorEntryConfig


def test_news_cache_ttl_defaults():
    cfg = OrchestratorEntryConfig()
    assert cfg.news_cache_ttl_seconds == 60.0
    assert cfg.news_cache_negative_ttl_seconds == 30.0


@pytest.mark.parametrize("field,bad", [
    ("news_cache_ttl_seconds", 0.0),
    ("news_cache_ttl_seconds", -1.0),
    ("news_cache_ttl_seconds", float("nan")),
    ("news_cache_negative_ttl_seconds", 0.0),
    ("news_cache_negative_ttl_seconds", float("inf")),
])
def test_news_cache_ttl_rejects_nonpositive_or_nonfinite(field, bad):
    with pytest.raises(ValueError):
        OrchestratorEntryConfig(**{field: bad})
