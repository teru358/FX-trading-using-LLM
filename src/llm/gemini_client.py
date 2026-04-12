from __future__ import annotations

"""Google Gemini API クライアント（REST / httpx 直呼び、SDK不要）。

# セットアップ
1. Google AI Studio (https://aistudio.google.com/) で API キーを取得
2. .env に設定:
   GEMINI_API_KEY=your_key_here
3. config/settings.yaml で切り替え:
   llm_provider: "gemini"
   gemini:
     model: "gemini-2.0-flash"

# 推奨モデル
- gemini-2.0-flash       : 高速・低コスト（デフォルト）
- gemini-1.5-flash       : Flash世代前モデル
- gemini-2.5-pro-preview : 高精度（レート制限に注意）
"""

import logging

import httpx
from tenacity import Retrying, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient, require_env

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClient(LLMClient):
    """Google Gemini generateContent API への LLM クライアント実装。"""

    def __init__(
        self,
        model: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._api_key = require_env("GEMINI_API_KEY", "gemini")

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def _do_chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        # system ロールを system_instruction に分離
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
            if m["role"] != "system"
        ]

        body: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if system_parts:
            body["system_instruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

        url = f"{_GEMINI_BASE}/{self._model}:generateContent"
        headers = {"x-goog-api-key": self._api_key}

        logger.debug(f"[LLM] Gemini({self._model}): generateContent ({len(messages)} messages)")
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries), wait=wait_fixed(10), reraise=True
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    resp.raise_for_status()

        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
