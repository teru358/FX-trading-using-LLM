from __future__ import annotations

import logging

import httpx
from tenacity import Retrying, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient

logger = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    """Ollama ローカルサーバーへの LLM クライアント実装。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        logger.debug(f"[LLM] Ollama({self._model}): /api/chat ({len(messages)} messages)")
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries), wait=wait_fixed(5), reraise=True
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
        return resp.json()["message"]["content"]
