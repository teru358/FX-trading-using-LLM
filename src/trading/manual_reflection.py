"""signal モードの manual 決済に対する reflection 生成。

internal 自動経路 (`_finalize_closed_orders`) と異なり、manual 決済は
session_store / adaptive_store / マクロ context を持たないため、最小の
コンテキスト (signal_reason のみ) で reflection を生成し、directional RAG に
complete フェーズとして登録する。
"""
from __future__ import annotations

import logging
from functools import partial

from src.analysis.price_analyzer import load_user_notes
from src.analysis.reflector import generate_close_reflection
from src.config import AppConfig
from src.llm.factory import create_llm_client
from src.rag.directional_writer import record_trade_complete
from src.rag.embedder import embed_text
from src.rag.vector_store import VectorStore
from src.trading.position_manager import Order

logger = logging.getLogger(__name__)


async def run_manual_reflection(
    config: AppConfig,
    store: VectorStore,
    closed_order: Order,
) -> None:
    """manual 決済の closed_order に対して reflection 生成と RAG 登録を実行する。

    失敗は警告ログに留め、例外は呼び出し側に伝播させない (BackgroundTasks から
    呼ばれる想定)。
    """
    pair_cfg = next(
        (p for p in config.tradeable_instruments if p.symbol == closed_order.pair),
        None,
    )
    if pair_cfg is None:
        logger.warning(f"[MANUAL/REFLECT] Unknown pair {closed_order.pair}, skipping reflection")
        return

    try:
        llm_reflect = create_llm_client(config, "reflection")
    except Exception as e:
        logger.warning(f"[MANUAL/REFLECT] LLM client unavailable: {e}")
        return

    embed_fn = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )

    try:
        reflection = await generate_close_reflection(
            pair_cfg=pair_cfg,
            order=closed_order,
            llm=llm_reflect,
            temperature=config.llm.reflection.temperature,
            user_notes=load_user_notes(config.user_notes_path, "reflect"),
            # manual 経路では session/ATR/macro context を持たないので全て空
            macro_context_at_entry="",
            entry_analysis="",
            sltp_comparison="",
            param_history="",
        )
    except Exception as e:
        logger.warning(f"[MANUAL/REFLECT] Reflection generation failed for {closed_order.order_id}: {e}")
        return

    logger.info(
        f"[MANUAL/REFLECT] {closed_order.pair} {closed_order.direction.upper()} "
        f"pnl={closed_order.realized_pnl or 0.0:+.2f} — reflection generated"
    )

    try:
        await record_trade_complete(
            store, embed_fn, closed_order,
            reflection.full_text if reflection else "",
        )
    except Exception as e:
        logger.warning(f"[MANUAL/REFLECT] RAG upsert failed for {closed_order.order_id}: {e}")
