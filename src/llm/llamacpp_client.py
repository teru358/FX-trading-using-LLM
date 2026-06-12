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

    @staticmethod
    def _fold_system_into_user(messages: list[dict]) -> list[dict]:
        """先頭の system メッセージを最初の user メッセージへ畳み込む。

        Gemma 系 (gemma-2 / gemma-3) の GGUF 内蔵チャットテンプレートは
        system ロールを ``raise_exception('System role not supported')`` で
        拒否し、llama-server はこれを HTTP 400 として返す。
        OpenAI 互換の他モデルでも system→user 結合は無害なため、
        llama.cpp 経路では一律に畳み込んで互換性を確保する。
        """
        if not messages or messages[0].get("role") != "system":
            return messages

        system_content = messages[0].get("content", "")
        rest = messages[1:]
        folded: list[dict] = []
        merged = False
        for msg in rest:
            if not merged and msg.get("role") == "user":
                folded.append({
                    "role": "user",
                    "content": f"{system_content}\n\n{msg.get('content', '')}",
                })
                merged = True
            else:
                folded.append(msg)
        if not merged:
            # user メッセージが無い場合は system を user として先頭に置く
            folded.insert(0, {"role": "user", "content": system_content})
        return folded

    async def _do_chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        payload = {
            "model": self._model,
            "messages": self._fold_system_into_user(messages),
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
                    if resp.is_error:
                        # llama-server はテンプレート/ctx 超過等の原因を本文に入れる。
                        # raise_for_status は本文を捨てるため、先にログへ残す。
                        logger.warning(
                            f"[LLM] llamacpp({self._model}): "
                            f"HTTP {resp.status_code} body: {resp.text[:500]!r}"
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
