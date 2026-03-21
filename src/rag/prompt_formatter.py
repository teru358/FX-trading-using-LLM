from __future__ import annotations


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


def format_news_for_prompt(news_entries: list[dict]) -> str:
    """RAG取得ニュースをOllamaプロンプト用テキストに整形する。"""
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
        lines.append(f"[{collected}] score={score:+.2f} | {summary}")
    return "\n".join(lines)
