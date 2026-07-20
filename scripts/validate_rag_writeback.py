"""Phase 4-1 検証: llamacpp embedding で書込→検索の E2E 動作確認。

現実の finance 動作と同じパスを通す:
1. make_embed_fn(config) で provider=llamacpp の embed_fn を生成
2. 専用テストコレクションに 3 エントリを書込
3. クエリを実行して書き込んだ内容が上位に来ることを確認
4. クリーンアップ

Usage:
    .venv/bin/python scripts/validate_rag_writeback.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb

from src.config import load_config
from src.rag.embedder import make_embed_fn


async def main() -> None:
    config = load_config()
    print(f"Config: embedding_provider={config.embedding.provider!r}")
    print(f"        db_path={config.rag_db_path}")

    embed_fn = make_embed_fn(config)

    # 一時コレクション (既存 RAG を汚さない)
    client = chromadb.PersistentClient(path=str(config.rag_db_path))
    col_name = "phase4_validation_tmp"
    try:
        client.delete_collection(col_name)
    except Exception:
        pass
    col = client.create_collection(col_name, metadata={"hnsw:space": "cosine"})
    print(f"Created temp collection: {col_name}")

    # 3 エントリを書込
    entries = [
        ("e1", "Fed raised interest rates by 0.25%. USD strengthened."),
        ("e2", "BOJ maintained ultra-loose monetary policy. Yen weakened further."),
        ("e3", "Oil prices surged due to Middle East tensions. Risk-off sentiment."),
    ]
    print("\nWriting entries...")
    for eid, text in entries:
        vec = await embed_fn(text=text)
        col.add(ids=[eid], embeddings=[vec], documents=[text])
        print(f"  [{eid}] dim={len(vec)} | {text[:60]}...")

    # 関連クエリ
    print("\nQuerying...")
    test_queries = [
        ("FOMC rate hike", "e1"),  # 期待: e1 が最上位
        ("日銀の緩和政策継続", "e2"),  # 期待: e2
        ("リスクオフ 原油", "e3"),  # 期待: e3
    ]
    all_pass = True
    for query, expected_top in test_queries:
        vec = await embed_fn(text=query)
        result = col.query(query_embeddings=[vec], n_results=3)
        top_id = result["ids"][0][0] if result["ids"] and result["ids"][0] else None
        top_dist = result["distances"][0][0] if result["distances"] else None
        marker = "✓" if top_id == expected_top else "✗"
        print(f"  {marker} Q={query!r} → top_id={top_id!r} (expected {expected_top!r}) dist={top_dist:.4f}")
        if top_id != expected_top:
            all_pass = False

    # クリーンアップ
    client.delete_collection(col_name)
    print(f"\nCleaned up temp collection: {col_name}")

    print(f"\n{'All tests PASSED' if all_pass else 'Some tests FAILED'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
