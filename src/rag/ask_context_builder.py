"""askコマンド用のコンテキスト構築。

ユーザーの質問からペアを抽出し、全データソースに対して
セマンティック検索を実行してコンテキストを構築する。
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial

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


def build_trade_summary(sessions: list, pairs: list[str]) -> str:
    if not sessions:
        return "=== Trade History ===\nNo trade history available."

    if pairs:
        filtered = [s for s in sessions if s.pair in pairs]
    else:
        filtered = list(sessions)

    if not filtered:
        return "=== Trade History ===\nNo trade history available."

    lines = []
    pair_groups: dict[str, list] = {}
    for s in filtered:
        pair_groups.setdefault(s.pair, []).append(s)

    label = "Overall" if not pairs else None
    for pair, group in pair_groups.items():
        header = label or pair
        wins = sum(1 for s in group if s.outcome == "win")
        losses = len(group) - wins
        total_pnl = sum(s.realized_pnl or 0 for s in group)
        avg_pnl = total_pnl / len(group) if group else 0
        win_rate = wins / len(group) * 100 if group else 0

        best = max(group, key=lambda s: s.realized_pnl or 0)
        worst = min(group, key=lambda s: s.realized_pnl or 0)
        recent = [s.outcome for s in group[-5:]]

        lines.append(f"=== Trade History: {header} ===")
        lines.append(f"Total: {len(group)} trades | Win: {wins} ({win_rate:.0f}%) | Loss: {losses}")
        lines.append(f"Total PnL: {total_pnl:+.2f} | Avg PnL: {avg_pnl:+.2f}")
        lines.append(f"Last {len(recent)}: {', '.join(recent)}")
        lines.append(f"Best: {best.realized_pnl:+.2f} ({best.close_reason}) | Worst: {worst.realized_pnl:+.2f} ({worst.close_reason})")

    return "\n".join(lines)


def build_forecast_accuracy(forecasts_by_pair: dict[str, list], pairs: list[str]) -> str:
    if not forecasts_by_pair:
        return "=== Forecast Accuracy ===\nNo forecast data available."

    target_pairs = pairs if pairs else list(forecasts_by_pair.keys())
    lines = ["=== Forecast Accuracy (24h) ==="]
    has_data = False

    for pair in target_pairs:
        records = forecasts_by_pair.get(pair, [])
        reviewed = [r for r in records if r.reviewed == 1 and r.latest_price_delta is not None]
        if not reviewed:
            continue
        has_data = True

        correct = 0
        for r in reviewed:
            delta = r.latest_price_delta
            if r.predicted_direction == "bullish" and delta > 0:
                correct += 1
            elif r.predicted_direction == "bearish" and delta < 0:
                correct += 1

        total = len(reviewed)
        accuracy = correct / total * 100 if total else 0
        lines.append(f"{pair}: {total} forecasts | Correct: {correct} ({accuracy:.0f}%) | Incorrect: {total - correct}")

    if not has_data:
        return "=== Forecast Accuracy ===\nNo forecast data available."

    return "\n".join(lines)
