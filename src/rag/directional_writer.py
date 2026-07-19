"""方向別 RAG (`store.directional`) への書き込み定型ロジックを集約する。

reflection サイクルから呼ばれる upsert を「何を書くか」だけで指定できるように
する。テキスト整形、embed → upsert、例外時のログをここに閉じ込める。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]


async def record_trade_complete(
    store: VectorStore,
    embed_fn: EmbedFn,
    closed_order: Any,
    reflection_text: str,
    horizon: str | None = None,
) -> None:
    """決済完了をコンプリートフェーズとして directional RAG に登録する。"""
    direction = "bullish" if closed_order.direction == "buy" else "bearish"
    realized = closed_order.realized_pnl or 0.0
    close_px = closed_order.close_price or closed_order.entry_price
    text = (
        f"{closed_order.pair} {direction} | "
        f"{closed_order.signal_reason} | "
        f"entry={closed_order.entry_price:.5f} "
        f"close={close_px:.5f} | "
        f"result={'win' if realized > 0 else 'loss'} "
        f"pnl={realized:+.2f} | "
        f"reason={closed_order.close_reason} | "
        f"{reflection_text}"
    )
    # strict: 失敗は例外を伝搬させ、呼び出し側 (reflection job) の retry 管理に
    # 委ねる (spec §3.5)。「未記録なのに成功扱い」を作らない。
    embedding = await embed_fn(text)
    # horizon キー無し = legacy swing カード規約 (spec V-1)。None は渡さない。
    extra = {"horizon": horizon} if horizon else {}
    store.directional.upsert(
        entry_id=f"{closed_order.order_id}_complete",
        text=text,
        embedding=embedding,
        direction=direction,
        pair=closed_order.pair,
        session_id=closed_order.order_id,
        session_type="trade",
        phase="complete",
        signal_score=0.0,
        confidence=0.0,
        outcome="win" if realized > 0 else "loss",
        realized_pnl=realized,
        close_reason=closed_order.close_reason,
        **extra,
    )
