from __future__ import annotations

"""llama.cpp (llama-server / llama-swap) OpenAI 互換 API クライアント。

# 前提
- llama-swap が OpenAI 互換エンドポイント (/v1/chat/completions) を公開
- llama-swap 内で複数モデルを自動スワップ管理 (TTL / LRU)
- モデル名は llama-swap config.yaml の models.<name> と一致させる

# セットアップ例 (config/settings.yaml)
    llm:
      llamacpp:
        base_url: "http://localhost:8080/v1"
        timeout_seconds: 300
        max_retries: 2
      news_analysis:
        provider: "llamacpp"
        model: "llama3.1-8b"
"""

import logging

import httpx
from tenacity import Retrying, retry_if_not_exception_type, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient

logger = logging.getLogger(__name__)


class LlamaCppClient(LLMClient):
    """llama-swap / llama-server への OpenAI 互換 HTTP クライアント。

    Ollama との違い:
      - エンドポイント: /v1/chat/completions (OpenAI 互換)
      - 認証: 不要 (ローカル実行のため)
      - モデルスワップは llama-swap が透過的に処理 (初回リクエストで load)
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int = 300,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "llamacpp"

    async def _do_chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        logger.debug(
            f"[LLM] llamacpp({self._model}): /v1/chat/completions ({len(messages)} messages)"
        )
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_fixed(5),
            retry=retry_if_not_exception_type(httpx.ReadTimeout),
            reraise=True,
        ):
            with attempt:
                # connect/pool は短く、read(LLM 推論 + モデルロード) は長く設定
                timeout = httpx.Timeout(
                    connect=10.0, read=self._timeout, write=10.0, pool=10.0,
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions", json=payload,
                    )
                    resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"]
        # deepseek-r1 等が <think>...</think> を返すケースに対応
        # reasoning_content も OpenAI 互換応答に含まれるが、現行は content のみ返す
        for eos in ("<|endoftext|>", "<|im_end|>", "<|eot_id|>"):
            if eos in content:
                content = content[: content.index(eos)]
                break
        return content.strip()
