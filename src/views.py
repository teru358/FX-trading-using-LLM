"""CLI / API から呼ばれる参照系ビュー関数群。

trading_cycle.py から責務分離したファイル。保存済みデータを読み出して
表示または応答するだけで、新規取得や実取引は行わない。

対応エンドポイント:
  run_news_view         — 保存済みニュースセンチメント表示
  run_tech_view         — 保存済みテクニカルスナップショット表示
  run_analysis_summary  — 総合分析サマリー (news × price 合成シグナル)
  run_forecast_view     — 直近24h の予測データテーブル表示
  run_ask               — FX 分析 LLM への質問応答 (insight を RAG 蓄積)
"""

from __future__ import annotations

import asyncio
import logging
import re as _re

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore, ForecastStore
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
    """保存済みテクニカルスナップショットを表示する（新規取得なし）。"""
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    snapshots_by_symbol = {}
    for inst in all_instruments:
        snaps = analysis_store.get_recent_ok_snapshots(
            inst.symbol, hours=config.rag.analysis_lookback_hours
        )
        if not snaps:
            latest = analysis_store.get_latest_snapshot(inst.symbol)
            snaps = [latest] if latest is not None else []
        snapshots_by_symbol[inst.symbol] = snaps
    display_names = {inst.symbol: inst.display_name for inst in all_instruments}
    print_tech_summary(snapshots_by_symbol, display_names, config.rag.analysis_lookback_hours)


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


# ── forecast view ─────────────────────────────────────────────


def run_forecast_view(config: AppConfig, forecast_store, pair_filter: str | None = None) -> None:
    """直近24h の予測データをテーブル表示する（新規取得なし）。"""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    console = Console()

    targets = [p for p in config.tradeable_instruments if pair_filter is None or p.symbol == pair_filter]
    if not targets:
        console.print(f"[red]対象ペアが見つかりません: {pair_filter}[/red]")
        return

    console.print("\n[bold cyan]=== Forecast Data (直近24h) ===[/bold cyan]")

    for inst in targets:
        records = forecast_store.get_recent_all(inst.symbol, hours=24)
        console.print(f"\n[bold]{inst.display_name}[/bold]")
        if not records:
            console.print("  [dim]データなし[/dim]")
            continue

        tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        tbl.add_column("生成時刻", style="dim")
        tbl.add_column("方向", justify="center")
        tbl.add_column("score", justify="right")
        tbl.add_column("conf", justify="right")
        tbl.add_column("最終検証時刻", style="dim")
        tbl.add_column("delta", justify="right")

        for r in records:
            direction_color = "green" if r.predicted_direction == "bullish" else ("red" if r.predicted_direction == "bearish" else "dim")
            score_color = "green" if r.combined_score > 0 else ("red" if r.combined_score < 0 else "dim")

            if r.reviewed == 3:
                # skipレコード
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    "[dim]–[/dim]",
                    "[dim]skip(score不足)[/dim]",
                )
            elif r.reviewed == 0 or r.latest_review_ts is None:
                # 未検証
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    "[dim]未検証[/dim]",
                    "[dim]–[/dim]",
                )
            else:
                # 検証済: deltaの色はbullish+正 or bearish+負なら緑、それ以外は赤
                delta = r.latest_price_delta or 0.0
                direction_match = (
                    (r.predicted_direction == "bullish" and delta > 0) or
                    (r.predicted_direction == "bearish" and delta < 0)
                )
                delta_color = "green" if direction_match else "red"
                tbl.add_row(
                    r.forecast_ts.strftime("%m-%d %H:%M"),
                    f"[{direction_color}]{r.predicted_direction}[/{direction_color}]",
                    f"[{score_color}]{r.combined_score:+.3f}[/{score_color}]",
                    f"{r.confidence:.2f}",
                    r.latest_review_ts.strftime("%m-%d %H:%M"),
                    f"[{delta_color}]{delta:+.5f}[/{delta_color}]",
                )

        console.print(tbl)

        forecast_records = [r for r in records if r.reviewed != 3]
        reviewed_records = [r for r in forecast_records if r.reviewed == 1]
        unreviewed_records = [r for r in forecast_records if r.reviewed == 0]
        skipped = [r for r in records if r.reviewed == 3]

        if reviewed_records:
            deltas = [r.latest_price_delta for r in reviewed_records if r.latest_price_delta is not None]
            avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
            direction_counts: dict[str, int] = {}
            for r in reviewed_records:
                direction_counts[r.predicted_direction] = direction_counts.get(r.predicted_direction, 0) + 1
            dir_summary = " ".join(f"{d}×{c}" for d, c in direction_counts.items())
            console.print(
                f"  avg_delta=[bold]{avg_delta:+.5f}[/bold] | {dir_summary} | "
                f"未検証: {len(unreviewed_records)}件 | skip: {len(skipped)}件"
            )
        else:
            console.print(f"  未検証: {len(unreviewed_records)}件 | skip: {len(skipped)}件")


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
    forecast_store = ForecastStore(config.prices_db_path)

    builder = AskContextBuilder(
        config=config,
        store=store,
        analysis_store=analysis_store,
        position_mgr=position_mgr,
        session_store=session_store,
        forecast_store=forecast_store,
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
