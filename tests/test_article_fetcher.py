"""article_fetcher.fetch_article_body() / fetch_bodies_concurrent() の単体テスト。

curl_cffi と trafilatura を mock して、各エラーパスとハッピーパスを検証する。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.article_fetcher import fetch_article_body, fetch_bodies_concurrent
from src.analysis.rss_fetcher import NewsItem


def _mock_async_session(response):
    """AsyncSession の async context manager を生成する helper。"""
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=session)
    async_ctx.__aexit__ = AsyncMock(return_value=None)
    return async_ctx, session


def _mock_response(status_code: int = 200, text: str = "<html><body>article</body></html>"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=RuntimeError(f"HTTP {status_code}")
        )
    else:
        resp.raise_for_status = MagicMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_fetch_article_body_success():
    """200 OK + trafilatura で本文抽出 → str を返す。"""
    response = _mock_response(200, "<html><body><p>The body text.</p></body></html>")
    async_ctx, _ = _mock_async_session(response)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value="The body text."):
        body = await fetch_article_body(
            "https://example.com/a",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body == "The body text."


@pytest.mark.asyncio
async def test_fetch_article_body_timeout_returns_none():
    """Timeout → None (例外を漏らさない)。"""
    session = MagicMock()
    session.get = AsyncMock(side_effect=TimeoutError("read timeout"))
    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=session)
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/timeout",
            timeout_seconds=1.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_4xx_returns_none():
    """403 Forbidden / paywall → None。"""
    response = _mock_response(403)
    async_ctx, _ = _mock_async_session(response)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/paywall",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_5xx_returns_none():
    """500 Internal Server Error → None。"""
    response = _mock_response(500)
    async_ctx, _ = _mock_async_session(response)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx):
        body = await fetch_article_body(
            "https://example.com/down",
            timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_article_body_extract_returns_none():
    """trafilatura.extract() が None を返したら body=None。"""
    response = _mock_response(200, "<html><body></body></html>")
    async_ctx, _ = _mock_async_session(response)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx), \
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
    async_ctx, _ = _mock_async_session(response)

    long_body = "x" * 10000
    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value=long_body):
        body = await fetch_article_body(
            "https://example.com/long",
            timeout_seconds=8.0, max_chars=500, user_agent="test/1.0",
        )
    assert body is not None
    assert len(body) == 500


@pytest.mark.asyncio
async def test_fetch_article_body_passes_user_agent_and_impersonate():
    """User-Agent ヘッダーと impersonate=chrome が session.get() に渡される。"""
    response = _mock_response(200)
    async_ctx, session = _mock_async_session(response)

    with patch("src.analysis.article_fetcher.AsyncSession", return_value=async_ctx), \
         patch("src.analysis.article_fetcher.trafilatura.extract", return_value="ok"):
        await fetch_article_body(
            "https://example.com/ua",
            timeout_seconds=8.0, max_chars=3000, user_agent="custom/2.0",
        )
    call_kwargs = session.get.call_args.kwargs
    assert call_kwargs["headers"]["User-Agent"] == "custom/2.0"
    assert call_kwargs["impersonate"] == "chrome"


@pytest.mark.asyncio
async def test_fetch_bodies_concurrent_writes_in_place():
    """各 NewsItem の body が in-place で書き込まれる。"""
    items = [
        NewsItem(title=f"t{i}", summary="s", source="x", published=None,
                 age_hours=None, link=f"https://example.com/{i}")
        for i in range(3)
    ]

    async def _fake_fetch(url, **kwargs):
        return f"body_for_{url.rsplit('/', 1)[1]}"

    with patch("src.analysis.article_fetcher.fetch_article_body", side_effect=_fake_fetch):
        await fetch_bodies_concurrent(
            items,
            max_concurrent=3, timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )

    bodies = [item.body for item in items]
    assert bodies == ["body_for_0", "body_for_1", "body_for_2"]


@pytest.mark.asyncio
async def test_fetch_bodies_concurrent_skips_items_without_link():
    """link が空の記事は fetch_article_body を呼ばない。"""
    items = [
        NewsItem(title="t1", summary="s", source="x", published=None, age_hours=None, link=""),
        NewsItem(title="t2", summary="s", source="x", published=None, age_hours=None,
                 link="https://example.com/2"),
    ]

    fetch_mock = AsyncMock(return_value="body")
    with patch("src.analysis.article_fetcher.fetch_article_body", new=fetch_mock):
        await fetch_bodies_concurrent(
            items,
            max_concurrent=3, timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )

    assert items[0].body is None  # link 空 → fetch されず None のまま
    assert items[1].body == "body"
    # fetch_article_body は 1 回だけ呼ばれた (link 付き記事のみ)
    assert fetch_mock.call_count == 1


@pytest.mark.asyncio
async def test_fetch_bodies_concurrent_respects_semaphore():
    """max_concurrent を超える並列実行が起きない。"""
    items = [
        NewsItem(title=f"t{i}", summary="s", source="x", published=None,
                 age_hours=None, link=f"https://example.com/{i}")
        for i in range(5)
    ]

    in_flight = 0
    peak = 0

    async def _slow_fetch(url, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return "body"

    with patch("src.analysis.article_fetcher.fetch_article_body", side_effect=_slow_fetch):
        await fetch_bodies_concurrent(
            items,
            max_concurrent=2, timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )

    assert peak <= 2  # 同時に 2 を超えない


@pytest.mark.asyncio
async def test_fetch_bodies_concurrent_continues_on_individual_failure():
    """1 記事の例外が他の記事の処理を止めない。"""
    items = [
        NewsItem(title=f"t{i}", summary="s", source="x", published=None,
                 age_hours=None, link=f"https://example.com/{i}")
        for i in range(3)
    ]

    async def _maybe_fail(url, **kwargs):
        if url.endswith("/1"):
            raise RuntimeError("boom")
        return "body"

    with patch("src.analysis.article_fetcher.fetch_article_body", side_effect=_maybe_fail):
        await fetch_bodies_concurrent(
            items,
            max_concurrent=3, timeout_seconds=8.0, max_chars=3000, user_agent="test/1.0",
        )

    # 失敗した index 1 は body=None、他は body="body"
    assert items[0].body == "body"
    assert items[1].body is None
    assert items[2].body == "body"
