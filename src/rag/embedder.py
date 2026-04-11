from __future__ import annotations

import logging

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# 既定値: Ollama 側が LLM 推論で詰まっていても落ちないよう長めに設定。
# 呼び出し側で timeout_seconds を指定可能。
_DEFAULT_TIMEOUT_SEC = 60.0
_MAX_ATTEMPTS = 3


async def embed_text(
    text: str,
    ollama_base_url: str,
    model: str = "nomic-embed-text",
    timeout_seconds: float = _DEFAULT_TIMEOUT_SEC,
) -> list[float]:
    """nomic-embed-text でテキストをベクトル化する（Ollama経由）。

    ReadTimeout / ConnectError などは tenacity で指数バックオフ付きリトライする。
    embedding はべき等かつ短時間のため、LLM 推論とは違い安全にリトライ可能。
    """
    # connect/pool は短く、read(Ollama 応答待ち) は長めに設定
    timeout = httpx.Timeout(
        connect=10.0, read=timeout_seconds, write=10.0, pool=10.0,
    )

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(
                (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError)
            ),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{ollama_base_url}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    resp.raise_for_status()
                    vector = resp.json()["embedding"]
    except RetryError as e:
        logger.warning(
            f"[EMBED] failed after {_MAX_ATTEMPTS} attempts: {e.last_attempt.exception()}"
        )
        raise

    logger.debug(f"Embedded {len(text)} chars → {len(vector)}-dim vector")
    return vector
