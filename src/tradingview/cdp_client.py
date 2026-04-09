"""Chrome DevTools Protocol クライアント。

TradingView Desktop (Electron) に CDP で接続し、
JavaScript を評価する最小限のクライアント。
"""
from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_TV_URL_PATTERN = re.compile(r"tradingview\.com/chart", re.IGNORECASE)


class CDPClient:
    """TradingView Desktop への CDP 接続を管理する。"""

    def __init__(self, host: str = "localhost", port: int = 9222) -> None:
        self._host = host
        self._port = port
        self._ws = None
        self._msg_id = 0

    async def _discover_target(self) -> str | None:
        """CDP ターゲット一覧から TradingView チャートページの WebSocket URL を返す。"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://{self._host}:{self._port}/json/list")
                resp.raise_for_status()
                targets = resp.json()
        except Exception as e:
            logger.debug(f"[TV] CDP discovery failed: {e}")
            return None

        for t in targets:
            if t.get("type") == "page" and _TV_URL_PATTERN.search(t.get("url", "")):
                return t.get("webSocketDebuggerUrl")
        return None

    async def connect(self) -> bool:
        """TradingView Desktop に接続する。成功すれば True。"""
        ws_url = await self._discover_target()
        if not ws_url:
            logger.info("[TV] TradingView Desktop not found, skipping")
            return False

        try:
            import websockets
            self._ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
            await self._send("Runtime.enable", {})
            logger.info("[TV] Connected to TradingView Desktop")
            return True
        except Exception as e:
            logger.warning(f"[TV] CDP connection failed: {e}")
            self._ws = None
            return False

    async def disconnect(self) -> None:
        """CDP 接続を閉じる。"""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send(self, method: str, params: dict) -> dict:
        """CDP コマンドを送信して結果を返す。"""
        if not self._ws:
            raise RuntimeError("Not connected")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params}
        await self._ws.send(json.dumps(msg))

        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                return data.get("result", {})

    async def evaluate(self, expression: str, await_promise: bool = False) -> any:
        """JavaScript 式を評価して結果を返す。"""
        result = await self._send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            desc = (result["exceptionDetails"].get("exception", {}).get("description")
                    or result["exceptionDetails"].get("text", "Unknown error"))
            raise RuntimeError(f"JS evaluation error: {desc}")
        return result.get("result", {}).get("value")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None
