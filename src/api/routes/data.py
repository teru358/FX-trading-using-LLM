"""データ参照系のエンドポイント。

GET /news      — カテゴリ別の最新ニュースセンチメント
GET /tech      — 銘柄別の最新収集行 (全 status) + 最新 ok 行 (run tech と同等)
GET /analyze   — 保存済みスナップショット + ニュースから総合シグナル算出 (LLM 不使用)
GET /feeds     — RSS フィード疎通確認
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from src.api._state import state, verify_api_key
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager
from src.utils.json_safe import load_json_column

router = APIRouter()


@router.get("/news", dependencies=[Depends(verify_api_key)])
def news() -> dict[str, Any]:
    """カテゴリ別の最新ニュースセンチメント (run news と同等)。"""
    assert state.store is not None and state.config is not None

    result: dict[str, Any] = {}
    for category in ("fx", "global", "japan"):
        entries = state.store.get_recent_category_news(
            categories=[category],
            lookback_hours=state.config.rag.news_lookback_hours,
        )
        if entries:
            latest = entries[0]["metadata"]
            result[category] = {
                "sentiment_score": latest.get("sentiment_score"),
                "confidence": latest.get("confidence"),
                "summary": latest.get("summary", ""),
                "key_themes": latest.get("key_themes", ""),
                "news_count": latest.get("news_count"),
                "collected_at": latest.get("collected_at"),
            }
        else:
            result[category] = None

    return {"lookback_hours": state.config.rag.news_lookback_hours, "categories": result}


@router.get("/tech", dependencies=[Depends(verify_api_key)])
def tech() -> dict[str, Any]:
    """銘柄別の最新収集行 (全 status) と最新 ok 行 (run tech と同等)。"""
    assert state.config is not None and state.analysis_store is not None

    all_instruments = (
        state.config.watch_only_instruments + state.config.tradeable_instruments
    )
    snapshots = []
    for inst in all_instruments:
        latest_collect = state.analysis_store.get_latest_collect_row(inst.symbol)
        latest_ok = state.analysis_store.get_latest_ok_row(inst.symbol)

        latest_collect_dict = None
        if latest_collect is not None:
            latest_collect_dict = {
                "analyzed_at": latest_collect.analyzed_at.isoformat() if latest_collect.analyzed_at else None,
                "collect_status": latest_collect.collect_status,
                "reason": latest_collect.reason,
            }

        latest_ok_dict = None
        if latest_ok is not None:
            latest_ok_dict = {
                "analyzed_at": latest_ok.analyzed_at.isoformat() if latest_ok.analyzed_at else None,
                "direction_bias": latest_ok.direction_bias,
                "bias_score": latest_ok.bias_score,
                "confidence": latest_ok.confidence,
                "mtf_alignment": latest_ok.mtf_alignment,
                "patterns": load_json_column(latest_ok.patterns_json, []),
            }

        snapshots.append({
            "symbol": inst.symbol,
            "display_name": inst.display_name,
            "mode": getattr(inst, "mode", None),
            "latest_collect": latest_collect_dict,
            "latest_ok": latest_ok_dict,
        })

    return {"snapshots": snapshots}


@router.get("/analyze", dependencies=[Depends(verify_api_key)])
async def analyze() -> dict[str, Any]:
    """保存済みスナップショット + ニュースから総合シグナルを返す (新規 LLM 取得なし)。"""
    assert state.config is not None and state.store is not None and state.analysis_store is not None

    from src.cycles._helpers import _summarize_pair

    state_store = StateStore(state.config.state_dir)
    pm = PositionManager(state_store, context="API_Analyze")

    results = await asyncio.gather(
        *[_summarize_pair(p, state.config, pm, state.store, state.analysis_store)
          for p in state.config.tradeable_instruments],
        return_exceptions=True,
    )
    signals = [r for r in results if r is not None and not isinstance(r, Exception)]

    if not signals:
        return {"signals": [], "message": "No snapshots available. Run 'run tech' first."}

    output = []
    for sig in signals:
        output.append({
            "pair": sig.pair,
            "action": sig.action,
            "predicted_direction": sig.predicted_direction,
            "combined_score": round(sig.combined_score, 4),
            "confidence": round(sig.confidence, 4),
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "position_size": sig.position_size,
            "signal_reason": sig.signal_reason,
            "news_score": round(sig.news.sentiment_score, 4),
            "news_confidence": round(sig.news.confidence, 4),
            "price_score": round(sig.price.bias_score, 4),
            "price_confidence": round(sig.price.confidence, 4),
            "generated_at": sig.generated_at.isoformat(),
        })

    return {"signals": output}


@router.get("/feeds", dependencies=[Depends(verify_api_key)])
def feeds() -> dict[str, Any]:
    """RSS フィード疎通確認 (カテゴリ別・フィード別の状態を返す)。"""
    import feedparser
    from src.analysis.rss_fetcher import feed_short_name

    assert state.config is not None

    global_kw = frozenset(k.lower() for k in state.config.keywords.global_keywords)
    japan_kw = frozenset(k.lower() for k in state.config.keywords.japan_keywords)

    categories_def = [
        ("FX",     state.config.news_sources.feeds_fx,     None),
        ("Global", state.config.news_sources.feeds_global, global_kw),
        ("Japan",  state.config.news_sources.feeds_japan,  japan_kw),
    ]

    result: dict[str, Any] = {}
    for label, feed_urls, keywords in categories_def:
        feed_results = []
        feeds_ok = 0
        filtered_count = 0

        for feed_url in feed_urls:
            name = feed_short_name(feed_url)
            try:
                parsed = feedparser.parse(feed_url)
                count = len(parsed.entries)
                latest_title = parsed.entries[0].get("title", "") if parsed.entries else ""
                feed_status = "ok" if count > 0 else "empty"
                feeds_ok += 1

                # Count keyword-filtered entries (simplified: check title+summary contains keyword)
                if keywords is None:
                    # FX: no keyword filter, count up to 5 recent entries
                    filtered_count += min(count, 5)
                else:
                    for entry in parsed.entries[:20]:
                        title = entry.get("title", "")
                        body = entry.get("summary", "")[:600]
                        combined = (title + " " + body).lower()
                        if any(kw in combined for kw in keywords):
                            filtered_count += 1

            except Exception:
                count = 0
                latest_title = ""
                feed_status = "failed"

            feed_results.append({
                "name": name,
                "status": feed_status,
                "count": count,
                "latest_title": latest_title[:80],
            })

        result[label] = {
            "feeds": feed_results,
            "filtered_count": filtered_count,
            "feeds_ok": feeds_ok,
            "total_feeds": len(feed_urls),
        }

    return {"categories": result}
