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

# 既定値: ローカル LLM 推論で詰まっていても落ちないよう長めに設定。
_DEFAULT_TIMEOUT_SEC = 60.0
_MAX_ATTEMPTS = 3


async def embed_text(
    text: str,
    ollama_base_url: str = "",
    model: str = "nomic-embed-text",
    timeout_seconds: float = _DEFAULT_TIMEOUT_SEC,
    provider: str = "ollama",
    base_url: str = "",
) -> list[float]:
    """テキストをベクトル化する。

    Args:
        text: ベクトル化対象テキスト
        ollama_base_url: 後方互換用。provider="ollama" 時の URL 指定
        model: 埋め込みモデル名 (Ollama: "nomic-embed-text" / llamacpp: llama-swap の model 名)
        timeout_seconds: HTTP 読み取りタイムアウト
        provider: "ollama" | "llamacpp"
        base_url: llamacpp 時の API URL (e.g. "http://localhost:8080/v1")

    ReadTimeout / ConnectError などは tenacity で指数バックオフ付きリトライする。
    embedding はべき等かつ短時間のため、LLM 推論とは違い安全にリトライ可能。
    """
    # connect/pool は短く、read(推論待ち) は長めに設定
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
                    if provider == "llamacpp":
                        # OpenAI 互換 /v1/embeddings
                        url = f"{(base_url or 'http://localhost:8080/v1').rstrip('/')}/embeddings"
                        resp = await client.post(
                            url,
                            json={"model": model, "input": text},
                        )
                        resp.raise_for_status()
                        vector = resp.json()["data"][0]["embedding"]
                    else:
                        # Ollama /api/embeddings
                        resp = await client.post(
                            f"{ollama_base_url.rstrip('/')}/api/embeddings",
                            json={"model": model, "prompt": text},
                        )
                        resp.raise_for_status()
                        vector = resp.json()["embedding"]
    except RetryError as e:
        logger.warning(
            f"[EMBED] failed after {_MAX_ATTEMPTS} attempts: {e.last_attempt.exception()}"
        )
        raise

    logger.debug(f"Embedded {len(text)} chars → {len(vector)}-dim vector ({provider})")
    return vector


def make_embed_fn(config) -> "callable":
    """config から embed_text の partial を生成するヘルパ。

    callers 側で provider 分岐を気にせず使える:
        embed_fn = make_embed_fn(config)
        vec = await embed_fn(text="...")

    Usage (既存の partial 使用箇所の置き換え):
        embed_fn = partial(embed_text, ollama_base_url=..., model=...)
        ↓
        embed_fn = make_embed_fn(config)
    """
    from functools import partial
    # 後方互換: テストで SimpleNamespace を渡す場合などに備えて getattr で取得
    provider = getattr(config.rag, "embedding_provider", "ollama")
    if provider == "llamacpp":
        llamacpp_cfg = getattr(config.llm, "llamacpp", None)
        base_url = getattr(llamacpp_cfg, "base_url", "http://localhost:8080/v1")
        return partial(
            embed_text,
            provider="llamacpp",
            base_url=base_url,
            model=config.rag.embedding_model,
        )
    return partial(
        embed_text,
        provider="ollama",
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )
