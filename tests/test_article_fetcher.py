"""article_fetcher.fetch_article_body() / fetch_bodies_concurrent() の単体テスト。

httpx と trafilatura を mock して、各エラーパスとハッピーパスを検証する。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.analysis.article_fetcher import fetch_article_body
from src.analysis.rss_fetcher import NewsItem


def _mock_async_client(response):
    """AsyncClient の async context manager を生成する helper。"""
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client)
    async_ctx.__aexit__ = AsyncMock(return_value=None)
    return async_ctx, client


def _mock_response(status_code: int = 200, text: str = "<html><body>article</body></html>"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        )
    else:
        resp.raise_for_status = MagicMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_fetch_article_body_success():
    """200 OK + trafilatura で本文抽出 → str を返す。"""
    response = _mock_response(200, "<html><body><p>The body text.</p></body></html>")
    async_ctx, client = _mock_async_client(response)

    with patch("httpx.AsyncClient", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value="The body text."):
        body = await fetch_article_body(
            "https://example.com/a",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body == "The body text."


@pytest.mark.asyncio
async def test_fetch_article_body_timeout_returns_none():
    """ReadTimeout → None (例外を漏らさない)。"""
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(
        side_effect=httpx.ReadTimeout("read timeout")
    )))
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/timeout",
            timeout_seconds=1.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_4xx_returns_none():
    """403 Forbidden / paywall → None。"""
    response = _mock_response(403)
    async_ctx, _ = _mock_async_client(response)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/paywall",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_5xx_returns_none():
    """500 Internal Server Error → None。"""
    response = _mock_response(500)
    async_ctx, _ = _mock_async_client(response)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/down",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_extract_returns_none():
    """trafilatura.extract() が None を返したら body=None。"""
    response = _mock_response(200, "<html><body></body></html>")
    async_ctx, _ = _mock_async_client(response)

    with patch("httpx.AsyncClient", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value=None):
        body = await fetch_article_body(
            "https://example.com/empty",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_truncates_to_max_chars():
    """本文が max_chars を超えたら切り詰める。"""
    response = _mock_response(200)
    async_ctx, _ = _mock_async_client(response)

    long_body = "x" * 10000
    with patch("httpx.AsyncClient", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value=long_body):
        body = await fetch_article_body(
            "https://example.com/long",
            timeout_seconds=8.0, max_chars=500, user_agent="test/1.0",
        )
    assert body is not None
    assert len(body) == 500


@pytest.mark.asyncio
async def test_fetch_article_body_passes_user_agent_header():
    """User-Agent ヘッダーが正しく送られる。"""
    response = _mock_response(200)
    async_ctx, client = _mock_async_client(response)

    with patch("httpx.AsyncClient", return_value=async_ctx) as mock_client_class, \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value="ok"):
        await fetch_article_body(
            "https://example.com/ua",
            timeout_seconds=8.0, max_chars=3000, user_agent="custom/2.0",
        )
    # AsyncClient が User-Agent を含む headers で生成されたか
    call_kwargs = mock_client_class.call_args.kwargs
    assert call_kwargs["headers"]["User-Agent"] == "custom/2.0"
