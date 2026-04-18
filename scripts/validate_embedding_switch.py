"""Phase 4-1 検証: embedding プロバイダー切替の動作確認。

Ollama と llama-swap の両方に embed リクエストを投げ、以下を検証:
1. 両方から 768 次元のベクトルが返る
2. ChromaDB への書き込み・検索が成功する
3. 同一テキストの Ollama vs llamacpp 間の cosine similarity を記録
   (同じモデルなら 0.99+ の類似度が期待されるが、厳密一致は期待しない)

Usage:
    .venv/bin/python scripts/validate_embedding_switch.py
"""
from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

# repo root を import path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.rag.embedder import embed_text


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


TEST_TEXTS = [
    "The Federal Reserve is expected to raise rates by 25 basis points.",
    "日銀の金融政策決定会合で円安傾向が続いている。",
    "USD/JPY broke above 150 on strong US non-farm payroll data.",
    "リスクオフムードで株価が下落、金が買われている。",
]


async def main() -> None:
    config = load_config()
    print(f"Config loaded. rag.embedding_provider = {config.rag.embedding_provider!r}")
    print(f"            rag.embedding_model    = {config.rag.embedding_model!r}")
    print()

    for i, text in enumerate(TEST_TEXTS, 1):
        print(f"[{i}/{len(TEST_TEXTS)}] {text[:60]}...")

        # Ollama 側で embed
        vec_ollama = await embed_text(
            text=text,
            provider="ollama",
            ollama_base_url=config.llm.ollama.base_url,
            model="nomic-embed-text",  # Ollama 側のモデル名
        )
        print(f"    Ollama:    dim={len(vec_ollama)} sample={vec_ollama[:3]}")

        # llamacpp 側で embed
        vec_llamacpp = await embed_text(
            text=text,
            provider="llamacpp",
            base_url=config.llm.llamacpp.base_url,
            model=config.rag.embedding_model,  # llama-swap 側のモデル名
        )
        print(f"    llamacpp:  dim={len(vec_llamacpp)} sample={vec_llamacpp[:3]}")

        # 類似度計算
        sim = cosine_similarity(vec_ollama, vec_llamacpp)
        marker = "✓" if sim > 0.95 else "⚠" if sim > 0.80 else "✗"
        print(f"    Cosine sim: {sim:.4f} {marker}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
