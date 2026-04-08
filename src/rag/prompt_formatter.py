from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import InstrumentConfig
    from src.data.analysis_store import _TechnicalSnapshot


def format_previous_analysis_for_prompt(snapshot: "_TechnicalSnapshot | None") -> str:
    """前回のテクニカル分析スナップショットを1行サマリーに整形する。"""
    if snapshot is None:
        return ""
    ts = snapshot.analyzed_at.strftime("%Y-%m-%d %H:%M") if snapshot.analyzed_at else "?"
    return (
        f"=== Previous Analysis ===\n"
        f"[{ts}] direction={snapshot.direction_bias}, "
        f"bias={snapshot.bias_score:+.2f}, confidence={snapshot.confidence:.2f}"
    )


def format_macro_context_for_prompt(
    snapshots: list["_TechnicalSnapshot"],
    instruments: list["InstrumentConfig"],
    realtime_provider: str = "yfinance",
) -> str:
    """監視専用銘柄（指数・参照FX等）のスナップショットをマクロコンテキストに整形する。"""
    if not snapshots:
        return ""
    name_map = {i.symbol: i.display_name for i in instruments}
    lines = [
        "=== Macro Reference Instruments ===",
        "Note: Rising equity indices generally indicate risk-on sentiment.",
        "Note: Bond ETFs (IEF, SHY) have INVERSE relationship to yields — rising ETF = falling rates.",
    ]
    if realtime_provider != "yfinance":
        lines.append(
            "(注意: 以下の監視銘柄はyfinance経由のため最大15-20分の遅延あり。"
            "取引ペアはリアルタイムデータ。相関判断時は時間差を考慮すること)"
        )
    for snap in snapshots:
        ts = snap.analyzed_at.strftime("%Y-%m-%d %H:%M") if snap.analyzed_at else "?"
        name = name_map.get(snap.symbol, snap.symbol)
        source = "yfinance, ~15min delay" if realtime_provider != "yfinance" else ""
        suffix = f"  ({source})" if source else ""
        lines.append(
            f"[{ts}] {name:16s} direction={snap.direction_bias}, "
            f"bias={snap.bias_score:+.2f}, confidence={snap.confidence:.2f}{suffix}"
        )
    return "\n".join(lines)


def format_reflections_for_prompt(reflections: list[dict]) -> str:
    """振り返りリストをOllamaプロンプト用テキストに整形する。"""
    if not reflections:
        return "No previous trading reflections available yet."
    lines = ["=== Recent Trading Reflections ==="]
    for i, r in enumerate(reflections, 1):
        meta = r["metadata"]
        lines.append(
            f"{i}. [{meta.get('cycle_time', '?')}] "
            f"{meta.get('action', '?').upper()}: "
            f"{meta.get('outcome_summary', '')} "
            f"→ Lesson: {meta.get('lesson', '')}"
        )
    return "\n".join(lines)


def format_insights_for_prompt(insights: list[dict]) -> str:
    """askから得た洞察をプロンプト用テキストに整形する。"""
    if not insights:
        return ""
    lines = ["=== Trading Insights (from analysis) ==="]
    for i, r in enumerate(insights, 1):
        meta = r.get("metadata", {})
        pair = meta.get("pair", "?")
        itype = meta.get("insight_type", "general")
        created = meta.get("created_at", "?")
        if len(created) > 16:
            created = created[:16]
        lines.append(f"{i}. [{created}] ({pair}/{itype}) {r.get('text', '')}")
    return "\n".join(lines)


def format_news_for_prompt(news_entries: list[dict]) -> str:
    """RAG取得ニュースをOllamaプロンプト用テキストに整形する。

    カテゴリ別分析結果（category メタデータ）と
    旧ペア別分析結果（pair メタデータ）の両方に対応する。
    """
    if not news_entries:
        return "No recent news available in knowledge base."
    lines = ["=== Recent News & Sentiment (RAG) ==="]
    seen: set[str] = set()
    for entry in news_entries[:8]:
        meta = entry["metadata"]
        summary = meta.get("summary", entry.get("text", "")[:200])
        if summary in seen:
            continue
        seen.add(summary)
        collected = meta.get("collected_at", "?")[:16]
        score = meta.get("sentiment_score", 0)
        # カテゴリ or ペアのラベル
        label = meta.get("category", meta.get("pair", "?"))
        lines.append(f"[{collected}] [{label}] score={score:+.2f} | {summary}")
    return "\n".join(lines)
