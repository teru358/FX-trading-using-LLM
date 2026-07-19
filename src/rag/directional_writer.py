"""方向別 RAG (`store.directional`) への書き込み定型ロジックを集約する。

trading_cycle / hold_review などから繰り返し呼ばれていた
upsert 呼び出しを「何を書くか」だけで指定できるようにする。direction の
正規化、テキスト整形、embed → upsert、例外時のログまでをここに閉じ込める。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from src.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]


def _normalize_direction(value: str | None, fallback_score: float = 0.0) -> str:
    """predicted_direction 文字列を bullish/bearish に正規化する。

    long/buy → bullish, short/sell → bearish, それ以外は score 符号で判定。
    """
    if value in ("bullish", "long", "buy"):
        return "bullish"
    if value in ("bearish", "short", "sell"):
        return "bearish"
    return "bullish" if fallback_score >= 0 else "bearish"


# ── trade ─────────────────────────────────────────────────────────


async def record_trade_entry(
    store: VectorStore,
    embed_fn: EmbedFn,
    order: Any,
    signal: Any,
    horizon: str | None = None,
) -> None:
    """新規約定をエントリーフェーズとして directional RAG に登録する。"""
    direction = "bullish" if order.direction == "buy" else "bearish"
    text = (
        f"{order.pair} {direction} | score={signal.combined_score:+.3f} "
        f"conf={signal.confidence:.2f} | entry={order.entry_price:.5f} "
        f"SL={order.stop_loss:.5f} TP={order.take_profit:.5f} | "
        f"{signal.detail_reason}"
    )
    try:
        embedding = await embed_fn(text)
        # horizon キー無し = legacy swing カード規約 (spec V-1)。None は渡さない。
        extra = {"horizon": horizon} if horizon else {}
        store.directional.upsert(
            entry_id=f"{order.order_id}_entry",
            text=text,
            embedding=embedding,
            direction=direction,
            pair=order.pair,
            session_id=order.order_id,
            session_type="trade",
            phase="entry",
            signal_score=signal.combined_score,
            confidence=signal.confidence,
            **extra,
        )
    except Exception as e:
        logger.warning(f"[SESSION] RAG entry failed for {order.order_id}: {e}")


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


# ── hold ──────────────────────────────────────────────────────────


async def record_hold_review(
    store: VectorStore,
    embed_fn: EmbedFn,
    hold: Any,
    review_text: str,
    lesson: str,
    horizon: str | None = None,
) -> None:
    """HOLD 判断の検証結果を directional RAG に登録する。"""
    direction = _normalize_direction(
        getattr(hold, "predicted_direction", None),
        fallback_score=hold.signal_score,
    )
    try:
        embedding = await embed_fn(review_text)
        # horizon キー無し = legacy swing カード規約 (spec V-1)。None は渡さない。
        extra = {"horizon": horizon} if horizon else {}
        store.directional.upsert(
            entry_id=f"hold_{hold.pair}_{hold.id}_complete",
            text=review_text,
            embedding=embedding,
            direction=direction,
            pair=hold.pair,
            session_id=f"hold_{hold.id}",
            session_type="hold",
            phase="complete",
            signal_score=hold.signal_score,
            confidence=hold.confidence,
            outcome="correct" if "CORRECT" in lesson else "incorrect",
            **extra,
        )
    except Exception as e:
        logger.warning(f"[HOLD/DIR] {hold.pair}: {e}")
