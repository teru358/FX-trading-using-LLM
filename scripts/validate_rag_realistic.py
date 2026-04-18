"""Phase 4-1 追加検証: 実運用相当の長文クエリで RAG ランキングを確認。

短いクエリでは cross-lingual の揺らぎで順位が乱れることがあるため、
finance の実際の使い方に近い 100-200 文字程度のクエリで検証する。
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
    embed_fn = make_embed_fn(config)

    client = chromadb.PersistentClient(path=str(config.rag_db_path))
    col_name = "phase4_realistic_tmp"
    try:
        client.delete_collection(col_name)
    except Exception:
        pass
    col = client.create_collection(col_name, metadata={"hnsw:space": "cosine"})

    # 実運用に近いエントリ (finance の news/directional RAG 相当)
    entries = [
        ("fed", "USDJPY=X bullish | Fed raised policy rate by 25bp to 5.50%. "
                "Powell hinted at further hikes if inflation remains sticky. "
                "USD broadly strengthened against major currencies including JPY."),
        ("boj", "USDJPY=X bullish | BOJ maintained negative interest rate policy. "
                "Ueda said wage growth still insufficient. JGB yields suppressed. "
                "Yen depreciated to 151 amid widening rate differential."),
        ("oil", "CRUDE bullish | Oil prices jumped 3% on Middle East tensions. "
                "Risk-off sentiment pushed equities lower. Gold rallied as safe haven. "
                "USD/JPY also rose on flight to dollar."),
    ]
    for eid, text in entries:
        vec = await embed_fn(text=text)
        col.add(ids=[eid], embeddings=[vec], documents=[text])

    # リアルなクエリ
    queries = [
        ("Federal Reserve rate decision with hawkish dot plot suggests further "
         "tightening, USD strength continues against JPY", "fed"),
        ("日銀が超緩和政策を据え置き、植田総裁は賃金上昇がまだ不十分と発言。"
         "日米金利差拡大で円安進行、USDJPY は 151 円台を維持", "boj"),
        ("Middle East escalation drives oil prices higher, risk-off mood across "
         "asset classes, safe havens bid including gold and USD", "oil"),
    ]

    all_pass = True
    for query, expected in queries:
        vec = await embed_fn(text=query)
        result = col.query(query_embeddings=[vec], n_results=3)
        ids = result["ids"][0]
        dists = result["distances"][0]
        marker = "✓" if ids[0] == expected else "✗"
        print(f"{marker} expected={expected!r} | top={ids[0]!r} dist={dists[0]:.4f}")
        print(f"  ranks: {list(zip(ids, [f'{d:.4f}' for d in dists]))}")
        print(f"  query: {query[:80]}...")
        print()
        if ids[0] != expected:
            all_pass = False

    client.delete_collection(col_name)
    print(f"{'All PASSED' if all_pass else 'Some FAILED'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
