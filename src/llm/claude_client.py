from __future__ import annotations

"""Anthropic Claude Messages API クライアント（REST / httpx 直呼び、SDK不要）。

# セットアップ
1. https://console.anthropic.com/ で API キーを取得
2. .env に設定:
   ANTHROPIC_API_KEY=sk-ant-...
3. config/settings.yaml で指定:
   llm:
     price_analysis:
       provider: "claude"
       model: "claude-sonnet-4-6"   # モデル一覧は下記参照

# 推奨モデル
- claude-haiku-4-5-20251001   : 高速・低コスト（デフォルト）
- claude-sonnet-4-6           : バランス型
- claude-opus-4-6             : 最高精度
"""

import logging

import httpx
from tenacity import Retrying, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient, require_env

logger = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


class ClaudeClient(LLMClient):
    """Anthropic Claude Messages API への LLM クライアント実装。"""

    def __init__(
        self,
        model: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._api_key = require_env("ANTHROPIC_API_KEY", "claude")

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        # Anthropic は system を別フィールドに分離する必要がある
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        conv = [m for m in messages if m["role"] != "system"]

        body: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": conv,
            "temperature": temperature,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        logger.debug(f"[LLM] Claude({self._model}): /v1/messages ({len(messages)} messages)")
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries), wait=wait_fixed(10), reraise=True
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(_ANTHROPIC_URL, headers=headers, json=body)
                    resp.raise_for_status()

        return resp.json()["content"][0]["text"]
