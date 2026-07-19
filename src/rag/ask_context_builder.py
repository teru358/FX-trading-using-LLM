"""askコマンド用のコンテキスト構築。

ユーザーの質問からペアを抽出し、全データソースに対して
セマンティック検索を実行してコンテキストを構築する。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_JAPANESE_PAIR_MAP: dict[str, str] = {
    "ドル円": "USDJPY=X",
    "ユーロドル": "EURUSD=X",
    "ポンドドル": "GBPUSD=X",
    "ユーロ円": "EURJPY=X",
    "ポンド円": "GBPJPY=X",
}


def extract_pairs(message: str, instruments: list) -> list[str]:
    found: list[str] = []
    msg_lower = message.lower()

    for jp_name, symbol in _JAPANESE_PAIR_MAP.items():
        if jp_name in message:
            if symbol not in found:
                found.append(symbol)

    for inst in instruments:
        dn = inst.display_name
        dn_noslash = dn.replace("/", "").lower()
        dn_lower = dn.lower()
        sym_base = inst.symbol.replace("=X", "").lower()

        if dn_noslash in msg_lower or dn_lower in msg_lower or sym_base in msg_lower:
            if inst.symbol not in found:
                found.append(inst.symbol)

    return found


def merge_and_rank_results(results: list[dict], max_results: int = 10) -> list[dict]:
    results.sort(key=lambda r: r.get("distance", float("inf")))
    return results[:max_results]


class AskContextBuilder:
    """askコマンド用のコンテキストを構築する。"""

    def __init__(self, config, store, analysis_store, position_mgr) -> None:
        self._config = config
        self._store = store
        self._analysis_store = analysis_store
        self._position_mgr = position_mgr

    async def build(self, user_message: str) -> dict[str, str]:
        config = self._config
        all_instruments = config.watch_only_instruments + config.tradeable_instruments
        pairs = extract_pairs(user_message, all_instruments)

        from src.rag.embedder import make_embed_fn
        embed_fn = make_embed_fn(config)
        query_embedding = await embed_fn(text=user_message)

        semantic_results = await self._semantic_search(query_embedding, pairs)
        technical = self._build_technical_snapshots(pairs)
        news = self._build_news_context()
        positions = self._build_positions()

        return {
            "open_positions": positions,
            "semantic_results": semantic_results,
            "technical_snapshots": technical,
            "news_context": news,
        }

    async def _semantic_search(self, query_embedding: list[float], pairs: list[str]) -> str:
        store = self._store
        all_results: list[dict] = []

        # News collection: カテゴリ別の最新分析結果を取得 (ニュースはカテゴリ単位でのみ蓄積されている)
        try:
            for cat in ("fx", "global", "japan"):
                entries = store.get_recent_category_news(
                    [cat], lookback_hours=self._config.rag.news_lookback_hours,
                )
                for e in entries[:3]:
                    e["source"] = "news"
                    e["distance"] = 0.5
                    all_results.append(e)
        except Exception as e:
            logger.warning(f"[ASK] News search failed: {e}")

        # Reflections collection
        try:
            refl_col = store._reflections
            if refl_col.count() > 0:
                where = None
                if pairs and len(pairs) == 1:
                    where = {"pair": {"$eq": pairs[0]}}
                results = refl_col.query(
                    query_embeddings=[query_embedding],
                    n_results=min(3, refl_col.count()),
                    where=where,
                )
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    all_results.append({
                        "text": doc,
                        "metadata": results["metadatas"][0][i],
                        "distance": results["distances"][0][i] if results.get("distances") else 0.5,
                        "source": "reflection",
                    })
        except Exception as e:
            logger.warning(f"[ASK] Reflection search failed: {e}")

        # Insights collection
        try:
            insights_results = self._store.query_insights(
                query_embedding=query_embedding,
                pair=pairs[0] if len(pairs) == 1 else None,
                top_k=3,
                lookback_hours=72,
            )
            for r in insights_results:
                all_results.append({
                    "text": r["text"],
                    "metadata": r.get("metadata", {}),
                    "distance": r.get("distance", 0.5),
                    "source": "insight",
                })
        except Exception as e:
            logger.warning(f"[ASK] Insight search failed: {e}")

        # Directional collections (bullish + bearish)
        for direction in ("bullish", "bearish"):
            try:
                col = store.directional._collection(direction)
                if col.count() > 0:
                    query_params = {
                        "query_embeddings": [query_embedding],
                        "n_results": min(3, col.count()),
                    }
                    if pairs and len(pairs) == 1:
                        query_params["where"] = {"pair": {"$eq": pairs[0]}}
                    results = col.query(**query_params)
                    for i, doc in enumerate(results.get("documents", [[]])[0]):
                        meta = results["metadatas"][0][i]
                        all_results.append({
                            "text": doc,
                            "metadata": meta,
                            "distance": results["distances"][0][i] if results.get("distances") else 0.5,
                            "source": direction,
                        })
            except Exception as e:
                logger.warning(f"[ASK] {direction} search failed: {e}")

        ranked = merge_and_rank_results(all_results, max_results=10)

        if not ranked:
            return "=== Related Context ===\nNo related data found."

        lines = ["=== Related Context (by relevance) ==="]
        for r in ranked:
            source = r.get("source", "unknown")
            meta = r.get("metadata", {})
            text = r.get("text", "")[:200]
            session_type = meta.get("session_type", "")
            tag = session_type if session_type else source
            lines.append(f"[{tag}] {text}")

        return "\n".join(lines)

    def _build_technical_snapshots(self, pairs: list[str]) -> str:
        config = self._config
        all_instruments = config.watch_only_instruments + config.tradeable_instruments

        if pairs:
            instruments = [i for i in all_instruments if i.symbol in pairs]
            instruments += config.watch_only_instruments
            seen = set()
            unique = []
            for i in instruments:
                if i.symbol not in seen:
                    seen.add(i.symbol)
                    unique.append(i)
            instruments = unique
        else:
            instruments = all_instruments

        lines = ["=== Technical Snapshots ==="]
        for inst in instruments:
            snaps = self._analysis_store.get_recent_ok_snapshots(
                inst.symbol, hours=config.rag.analysis_lookback_hours,
            )
            if snaps:
                s = snaps[0]
                lines.append(
                    f"{inst.display_name}: bias={s.bias_score:+.2f} conf={s.confidence:.2f} "
                    f"dir={s.direction_bias} mtf={s.mtf_alignment if s.mtf_alignment is not None else '-'}"
                )
            else:
                lines.append(f"{inst.display_name}: no snapshot")
        return "\n".join(lines)

    def _build_news_context(self) -> str:
        lines = ["=== News Context ==="]
        for cat in ("fx", "global", "japan"):
            entries = self._store.get_recent_category_news(
                [cat], lookback_hours=self._config.rag.news_lookback_hours,
            )
            if entries:
                for e in entries[:3]:
                    meta = e.get("metadata", {})
                    summary = meta.get("summary") or e.get("text", "")[:80]
                    score = meta.get("sentiment_score", 0.0)
                    lines.append(f"[{cat}] {summary} (sentiment={score:+.2f})")
        return "\n".join(lines)

    def _build_positions(self) -> str:
        account = self._position_mgr.get_account_state()
        lines = ["=== Open Positions ==="]
        if account.open_positions:
            for pos in account.open_positions:
                lines.append(
                    f"{pos.pair} {pos.direction.upper()} entry={pos.entry_price:.5f} "
                    f"SL={pos.stop_loss:.5f} TP={pos.take_profit:.5f}"
                )
        else:
            lines.append("No open positions.")
        return "\n".join(lines)


