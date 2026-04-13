"""Chrome DevTools Protocol クライアント。

TradingView Desktop (Electron) に CDP で接続し、
JavaScript を評価する最小限のクライアント。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading

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
        # send/recv を直列化する asyncio.Lock (同一プロセス内の同時呼び出し用)
        # 使用時のイベントループで遅延生成する
        self._send_lock: asyncio.Lock | None = None
        # websocket / asyncio.Lock が束縛されているイベントループ。
        # 別ループから呼ばれたら接続状態をリセットする
        # (asyncio プリミティブは別ループを跨げないため)。
        self._bound_loop: asyncio.AbstractEventLoop | None = None

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
            self._bound_loop = asyncio.get_running_loop()
            self._send_lock = None  # 新しいループで遅延生成させる
            await self._send("Runtime.enable", {})
            logger.info("[TV] Connected to TradingView Desktop")
            return True
        except Exception as e:
            logger.warning(f"[TV] CDP connection failed: {e}")
            self._ws = None
            self._bound_loop = None
            return False

    async def disconnect(self) -> None:
        """CDP 接続を閉じる。別ループで生成された ws は close 不可なのでスキップ。"""
        if self._ws:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if self._bound_loop is None or current_loop is self._bound_loop:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            self._ws = None
            self._bound_loop = None
            self._send_lock = None

    async def ensure_connected(self) -> bool:
        """接続が生きていない場合は再接続を試みる。成功すれば True。

        呼び出し元が別イベントループの場合は旧接続を破棄して再接続する
        (websocket・asyncio.Lock は生成時のループに束縛されるため、
        別ループからは使用できない)。
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        if self._bound_loop is not None and self._bound_loop is not current_loop:
            # 旧ループは既に閉じている可能性が高いので disconnect は呼ばず
            # 参照だけ破棄する
            self._ws = None
            self._send_lock = None
            self._bound_loop = None

        if self._ws is not None:
            return True
        return await self.connect()

    async def _send(self, method: str, params: dict) -> dict:
        """CDP コマンドを送信して結果を返す。"""
        if not self._ws:
            raise RuntimeError("Not connected")
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            self._msg_id += 1
            msg_id = self._msg_id
            msg = {"id": msg_id, "method": method, "params": params}
            await self._ws.send(json.dumps(msg))
            while True:
                raw = await self._ws.recv()
                data = json.loads(raw)
                if data.get("id") == msg_id:
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


# ──────────────────────────────────────────────────────────────────────
# プロセス共有 CDPClient レジストリ
# ──────────────────────────────────────────────────────────────────────

_SHARED_CLIENTS: dict[tuple[str, int], CDPClient] = {}
_REGISTRY_LOCK = threading.Lock()


def get_shared_cdp_client(host: str, port: int) -> CDPClient:
    """(host, port) ごとに singleton の CDPClient を返す。

    最初の呼び出しで生成し、以降は同じインスタンスを再利用する。
    接続自体は遅延: 呼び出し側で ``ensure_connected()`` を実行する。
    """
    key = (host, port)
    with _REGISTRY_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            client = CDPClient(host=host, port=port)
            _SHARED_CLIENTS[key] = client
        return client


async def shutdown_shared_cdp_clients() -> None:
    """登録済みの共有 CDPClient を全て閉じる (プロセス終了時に呼び出す)。"""
    with _REGISTRY_LOCK:
        clients = list(_SHARED_CLIENTS.values())
        _SHARED_CLIENTS.clear()
    for c in clients:
        try:
            await c.disconnect()
        except Exception as e:
            logger.debug(f"[TV] Shared client disconnect error: {e}")
