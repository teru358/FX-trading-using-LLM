from __future__ import annotations

"""OpenAI Chat Completions API クライアント（REST / httpx 直呼び、SDK不要）。

# セットアップ
1. https://platform.openai.com/api-keys で API キーを取得
2. .env に設定:
   OPENAI_API_KEY=sk-...
3. config/settings.yaml で指定:
   llm:
     price_analysis:
       provider: "openai"
       model: "gpt-4o-mini"   # gpt-4o / o3-mini なども可

# 推奨モデル
- gpt-4o-mini  : 高速・低コスト（デフォルト）
- gpt-4o       : 高精度
- o3-mini      : 推論特化（reasoning系）
"""

import logging
import os

import httpx
from tenacity import Retrying, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions API への LLM クライアント実装。"""

    def __init__(
        self,
        model: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise EnvironmentError("OPENAI_API_KEY を .env に設定してください。")

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        # OpenAI は system/user/assistant ロールをそのまま受け付ける
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        logger.debug(f"[LLM] OpenAI({self._model}): chat/completions ({len(messages)} messages)")
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries), wait=wait_fixed(10), reraise=True
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(_OPENAI_URL, headers=headers, json=body)
                    resp.raise_for_status()

        return resp.json()["choices"][0]["message"]["content"]
