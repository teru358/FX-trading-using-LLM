from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def embed_text(
    text: str,
    ollama_base_url: str,
    model: str = "nomic-embed-text",
) -> list[float]:
    """nomic-embed-text でテキストをベクトル化する（Ollama経由）。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{ollama_base_url}/api/embeddings",
            json={"model": model, "prompt": text},
        )
        resp.raise_for_status()
    vector = resp.json()["embedding"]
    logger.debug(f"Embedded {len(text)} chars → {len(vector)}-dim vector")
    return vector
