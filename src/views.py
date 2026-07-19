"""CLI / API から呼ばれる参照系ビュー関数群。

trading_cycle.py から責務分離したファイル。保存済みデータを読み出して
表示または応答するだけで、新規取得や実取引は行わない。

対応エンドポイント:
  run_news_view         — 保存済みニュースセンチメント表示
  run_tech_view         — 保存済みテクニカルスナップショット表示
  run_analysis_summary  — 総合分析サマリー (news × price 合成シグナル)
  run_ask               — FX 分析 LLM への質問応答 (insight を RAG 蓄積)
"""

from __future__ import annotations

import asyncio
import logging
import re as _re

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.session_store import SessionStore
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.ask_context_builder import AskContextBuilder, extract_pairs
from src.rag.embedder import make_embed_fn
from src.rag.vector_store import VectorStore
from src.reporting.reporter import print_news_summary, print_run_summary, print_tech_summary
from src.trading.position_manager import PositionManager
from src.utils.clock import db_now, local_now

logger = logging.getLogger(__name__)


# ── news / tech / analysis summary ────────────────────────────


def run_news_view(config: AppConfig, store: VectorStore) -> None:
    """保存済みニュースセンチメントを表示する（新規取得なし）。"""
    entries_by_category = {
        cat: store.get_recent_category_news([cat], lookback_hours=config.rag.news_lookback_hours)
        for cat in ("fx", "global", "japan")
    }
    print_news_summary(entries_by_category, config.rag.news_lookback_hours)


def run_tech_view(config: AppConfig, analysis_store: AnalysisStore) -> None:
    """保存済みテクニカルスナップショットを表示する（新規取得なし）。

    全 status の最新行 (latest_collect) と ok 限定の最新行 (latest_ok) を
    並べて表示する。lookback 非依存なので休場中も最後の収集試行が見える。
    """
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    rows = []
    for inst in all_instruments:
        latest_collect = analysis_store.get_latest_collect_row(inst.symbol)
        latest_ok = analysis_store.get_latest_ok_row(inst.symbol)
        rows.append((inst, latest_collect, latest_ok))
    print_tech_summary(rows)


async def _analysis_summary(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """保存済みの最新分析結果を集約して総合分析サマリーを表示する。"""
    # trading_cycle 内のヘルパ (_summarize_pair) を再利用。views.py → trading_cycle.py
    # への片方向依存で循環を作らない (trading_cycle.py からは views.py を import しない)。
    from src.trading_cycle import _summarize_pair

    run_start = local_now(config)
    logger.info(f"=== Analysis summary: {run_start.strftime('%Y-%m-%d %H:%M %Z')} ===")

    results = await asyncio.gather(
        *[_summarize_pair(p, config, position_mgr, store, analysis_store)
          for p in config.tradeable_instruments],
        return_exceptions=True,
    )

    signals = [r for r in results if r is not None and not isinstance(r, Exception)]
    if not signals:
        logger.warning("表示できるシグナルがありません。先に run tech でテクニカル分析を実行してください。")

    account_state = position_mgr.get_account_state()
    print_run_summary(
        signals=signals,
        executed_orders=[],
        closed_this_run=[],
        account_state=account_state,
        run_start=run_start.replace(tzinfo=None),
    )


def run_analysis_summary(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> None:
    """CLI/API から呼び出す同期ラッパー。"""
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="AnalysisSummary")
    asyncio.run(_analysis_summary(config, position_mgr, store, analysis_store))


# ── ask (LLM 質問応答) ────────────────────────────────────────


async def _run_ask(
    user_message: str,
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> str:
    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, context="Ask")
    session_store = SessionStore(config.prices_db_path)

    builder = AskContextBuilder(
        config=config,
        store=store,
        analysis_store=analysis_store,
        position_mgr=position_mgr,
        session_store=session_store,
    )
    context_dict = await builder.build(user_message)

    llm = create_llm_client(config, "reflection")

    user_prompt = render_prompt(
        "ask_user.j2",
        user_message=user_message,
        **context_dict,
    )
    messages = [
        {"role": "system", "content": load_prompt("ask_system.txt")},
        {"role": "user", "content": user_prompt},
    ]
    logger.info(f"[ASK] LLM呼び出し中 ({len(user_message)} chars, context={len(user_prompt)} chars)...")
    response = await llm.chat(messages, temperature=config.llm.reflection.temperature)
    response = _re.sub(r"<think>.*?</think>", "", response, flags=_re.DOTALL).strip()

    # promoted 分岐で HTTP レスポンスが先に返った場合でも activity.log から辿れるよう
    # 回答プレビューを残す。長文はトリム (過度なログ肥大化を避ける)。
    _preview = response if len(response) <= 800 else response[:800] + "…(truncated)"
    logger.info(f"[ASK] 回答 ({len(response)} chars): {_preview}")

    # === Ask回答を洞察としてRAGに保存 ===
    try:
        all_instruments = config.watch_only_instruments + config.tradeable_instruments
        mentioned_pairs = extract_pairs(user_message, all_instruments)
        pair_label = mentioned_pairs[0] if mentioned_pairs else "GENERAL"

        # 回答の要約（長すぎる場合は先頭500文字）
        insight_text = response[:500] if len(response) > 500 else response

        embed_fn = make_embed_fn(config)
        insight_embedding = await embed_fn(
            text=f"Q: {user_message}\nA: {insight_text}",
        )

        now = db_now()
        entry_id = f"insight_{now.strftime('%Y%m%d_%H%M%S')}"

        # insight_type をキーワードで分類
        msg_lower = user_message.lower()
        if any(w in msg_lower for w in ["分析", "テクニカル", "チャート", "パターン"]):
            insight_type = "analysis"
        elif any(w in msg_lower for w in ["リスク", "損切", "ストップ"]):
            insight_type = "risk"
        elif any(w in msg_lower for w in ["振り返", "反省", "レビュー", "パフォーマンス"]):
            insight_type = "pattern"
        else:
            insight_type = "general"

        store.upsert_insight(
            entry_id=entry_id,
            text=insight_text,
            embedding=insight_embedding,
            pair=pair_label,
            insight_type=insight_type,
            source_question=user_message,
            created_at=now,
        )
    except Exception as e:
        logger.warning(f"[ASK] Failed to save insight: {e}")

    return response


def run_ask(
    user_message: str,
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> str:
    """CLI/API から呼び出す同期ラッパー。"""
    return asyncio.run(_run_ask(user_message, config, store, analysis_store))
