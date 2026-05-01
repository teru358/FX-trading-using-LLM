"""既存の trades.json と fx_reflections を方向別コレクションに移行する。

冪等性あり: 再実行しても重複しない（upsert使用）。

Usage:
    cd /home/teru/project/finance
    uv run python scripts/migrate_directional_rag.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.data.session_store import SessionStore
from src.rag.embedder import embed_text
from src.rag.vector_store import VectorStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_signal_reason(reason: str) -> tuple[float, float]:
    score, conf = 0.0, 0.0
    m_score = re.search(r"score=([+\-]?\d+\.?\d*)", reason)
    m_conf = re.search(r"conf=(\d+\.?\d*)", reason)
    if m_score:
        score = float(m_score.group(1))
    if m_conf:
        conf = float(m_conf.group(1))
    return score, conf


async def main():
    config = load_config()
    store = VectorStore(config.rag_db_path)
    session_store = SessionStore(config.prices_db_path)

    embed_fn = partial(
        embed_text,
        ollama_base_url=config.rag.embedding_base_url,
        model=config.rag.embedding_model,
    )

    # Step 1: trades.json → trading_sessions + directional ChromaDB
    trades_path = config.state_dir / "trades.json"
    if not trades_path.exists():
        logger.warning(f"trades.json not found at {trades_path}")
        return

    with open(trades_path, encoding="utf-8") as f:
        trades = json.load(f)

    logger.info(f"Migrating {len(trades)} trades...")

    bullish_count = 0
    bearish_count = 0

    for trade in trades:
        session_id = trade["order_id"]
        direction = "bullish" if trade["direction"] == "buy" else "bearish"
        score, conf = _parse_signal_reason(trade.get("signal_reason", ""))
        realized_pnl = trade.get("realized_pnl", 0.0)
        outcome = "win" if realized_pnl > 0 else "loss"

        existing = session_store.get_session(session_id)
        if existing is None:
            session_store.create_session(
                session_id=session_id,
                pair=trade["pair"],
                direction=direction,
                entry_price=trade["entry_price"],
                stop_loss=trade.get("stop_loss", 0.0),
                take_profit=trade.get("take_profit", 0.0),
                position_size=trade.get("position_size", 0.0),
                signal_score=score,
                signal_confidence=conf,
                macro_context=trade.get("macro_context_at_entry", ""),
                analysis_summary=trade.get("signal_reason", ""),
                opened_at=datetime.fromisoformat(trade["opened_at"]),
            )
            if trade.get("closed_at"):
                session_store.close_session(
                    session_id=session_id,
                    closed_at=datetime.fromisoformat(trade["closed_at"]),
                    close_price=trade.get("close_price", trade["entry_price"]),
                    close_reason=trade.get("close_reason", "manual"),
                    realized_pnl=realized_pnl,
                )

        macro_summary = trade.get("macro_context_at_entry", "")
        if macro_summary and len(macro_summary) > 200:
            macro_summary = macro_summary[:200] + "..."

        complete_text = (
            f"{trade['pair']} {direction} | score={score:+.3f} conf={conf:.2f} | "
            f"entry={trade['entry_price']:.5f} close={trade.get('close_price', 0):.5f} | "
            f"result={outcome} pnl={realized_pnl:+.2f} | "
            f"reason={trade.get('close_reason', 'unknown')} | "
            f"{macro_summary}"
        )

        embedding = await embed_fn(complete_text)
        store.directional.upsert(
            entry_id=f"{session_id}_complete",
            text=complete_text,
            embedding=embedding,
            direction=direction,
            pair=trade["pair"],
            session_id=session_id,
            session_type="trade",
            phase="complete",
            signal_score=score,
            confidence=conf,
            outcome=outcome,
            realized_pnl=realized_pnl,
            close_reason=trade.get("close_reason"),
        )

        if direction == "bullish":
            bullish_count += 1
        else:
            bearish_count += 1

    # Step 2: Existing fx_reflections → directional collections
    legacy_count = 0
    try:
        legacy_col = store._reflections
        all_entries = legacy_col.get(include=["documents", "metadatas", "embeddings"])
        ids = all_entries.get("ids", [])
        docs = all_entries.get("documents", [])
        metas = all_entries.get("metadatas", [])
        embeddings = all_entries.get("embeddings", [])

        for i, doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            action = meta.get("action", "")

            if action in ("bullish", "long", "buy"):
                dir_label = "bullish"
            elif action in ("bearish", "short", "sell"):
                dir_label = "bearish"
            else:
                doc_text = docs[i] if i < len(docs) else ""
                if "bullish" in doc_text.lower() or "buy" in doc_text.lower():
                    dir_label = "bullish"
                elif "bearish" in doc_text.lower() or "sell" in doc_text.lower():
                    dir_label = "bearish"
                else:
                    logger.debug(f"Skipping undetermined direction: {doc_id}")
                    continue

            emb = embeddings[i] if i < len(embeddings) else None
            if emb is None:
                continue

            store.directional.upsert(
                entry_id=f"legacy_{doc_id}",
                text=docs[i],
                embedding=emb,
                direction=dir_label,
                pair=meta.get("pair", "unknown"),
                session_id=f"legacy_{doc_id}",
                session_type="trade",
                phase="complete",
                signal_score=0.0,
                confidence=0.0,
            )
            legacy_count += 1
    except Exception as e:
        logger.warning(f"Legacy migration error: {e}")

    # Step 3: Verification
    logger.info("=== Migration Summary ===")
    logger.info(f"Trades migrated: {len(trades)}")
    logger.info(f"  bullish: {bullish_count}")
    logger.info(f"  bearish: {bearish_count}")
    logger.info(f"Legacy reflections migrated: {legacy_count}")
    logger.info(f"DirectionalStore bullish count: {store.directional.count('bullish')}")
    logger.info(f"DirectionalStore bearish count: {store.directional.count('bearish')}")

    test_emb = await embed_fn("EURUSD sell bearish")
    test_results = store.directional.query(test_emb, "bearish", top_k=1)
    if test_results:
        logger.info(f"Sample search OK: {test_results[0]['metadata'].get('session_id')}")
    else:
        logger.info("Sample search: no results (collection may be empty)")

    logger.info("=== Migration complete ===")


if __name__ == "__main__":
    asyncio.run(main())
