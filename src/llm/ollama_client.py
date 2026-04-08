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
                # connect/pool は短く、read(LLM推論待ち)は長く設定
                timeout = httpx.Timeout(connect=10.0, read=self._timeout, write=10.0, pool=10.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                    resp.raise_for_status()
        content = resp.json()["message"]["content"]
        # 一部モデルが停止トークン後に自己対話を続けるケースを切り捨て
        for eos in ("<|endoftext|>", "<|im_end|>", "<|eot_id|>"):
            if eos in content:
                content = content[:content.index(eos)]
                break
        return content.strip()
