"""CDPクライアントのテスト（httpx/websocketsをモック）。"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.tradingview.cdp_client import CDPClient


@pytest.mark.asyncio
async def test_discover_chart_target():
    """TradingViewチャートページを正しく発見する。"""
    mock_targets = [
        {"type": "page", "url": "https://www.tradingview.com/chart/abc123", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/1"},
        {"type": "page", "url": "https://www.google.com", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/2"},
    ]
    client = CDPClient(port=9222)
    with patch("httpx.AsyncClient") as mock_http:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_targets
        mock_resp.raise_for_status = MagicMock()
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http.return_value)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.return_value.get = AsyncMock(return_value=mock_resp)
        target = await client._discover_target()
    assert target == "ws://localhost:9222/devtools/page/1"


@pytest.mark.asyncio
async def test_discover_no_tradingview():
    """TradingViewページがない場合Noneを返す。"""
    mock_targets = [
        {"type": "page", "url": "https://www.google.com", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/2"},
    ]
    client = CDPClient(port=9222)
    with patch("httpx.AsyncClient") as mock_http:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_targets
        mock_resp.raise_for_status = MagicMock()
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http.return_value)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.return_value.get = AsyncMock(return_value=mock_resp)
        target = await client._discover_target()
    assert target is None
