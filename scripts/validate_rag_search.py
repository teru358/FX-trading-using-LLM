"""Phase 4-1 検証: llamacpp embedding で RAG 検索が正常動作するか。

既存 ChromaDB には Ollama 由来の embedding が格納されている。
新しい llamacpp embedding でクエリしても、意味的に近いエントリが
上位にヒットすることを確認する。

Usage:
    .venv/bin/python scripts/validate_rag_search.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.rag.embedder import make_embed_fn
from src.rag.vector_store import VectorStore


async def main() -> None:
    config = load_config()
    print(f"Config: embedding_provider={config.embedding.provider!r}")

    store = VectorStore(db_path=config.rag_db_path)

    # クエリ用 embed_fn (llamacpp 側)
    embed_fn = make_embed_fn(config)

    queries = [
        "FOMC 利上げ観測",
        "BOJ policy meeting",
        "USD/JPY 155 breakout",
    ]

    for q in queries:
        print(f"\nQuery: {q!r}")
        vec = await embed_fn(text=q)
        print(f"  embedded with {config.embedding.provider}: dim={len(vec)}")

        # directional RAG 検索
        try:
            bullish = store.directional.query(vec, direction="bullish", top_k=3)
            print(f"  bullish hits: {len(bullish)}")
            for i, hit in enumerate(bullish[:3], 1):
                text = (hit.get("text") or "")[:80]
                dist = hit.get("distance", "?")
                print(f"    [{i}] dist={dist:.4f} | {text}...")
        except Exception as e:
            print(f"  bullish search error: {e}")

        # insight RAG 検索 (存在すれば)
        try:
            ins = store.query_insights(vec, top_k=3)
            print(f"  insight hits: {len(ins)}")
            for i, hit in enumerate(ins[:3], 1):
                text = (hit.get("text") or "")[:80]
                dist = hit.get("distance", "?")
                print(f"    [{i}] dist={dist:.4f} | {text}...")
        except Exception as e:
            print(f"  insight search skipped: {e}")


if __name__ == "__main__":
    asyncio.run(main())
