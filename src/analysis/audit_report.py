"""audit レポートの markdown レンダラー。

各 Section を独立した関数で render し、main runner が連結する。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FEW_TRADES_THRESHOLD = 5


def render_section1_summary(sessions: list, period_days: int) -> str:
    """Section 1: 全体サマリ (勝率 / PF / DD / Sharpe)。"""
    lines = ["## Section 1: 全体サマリ", ""]
    n = len(sessions)

    if n == 0:
        lines.append("トレードなし。期間内にクローズ済みトレードはありません。")
        return "\n".join(lines)

    wins = [s for s in sessions if (s.realized_pnl or 0) > 0]
    losses = [s for s in sessions if (s.realized_pnl or 0) <= 0]
    win_rate = len(wins) / n if n > 0 else 0.0

    total_pnl = sum(s.realized_pnl or 0 for s in sessions)
    gross_profit = sum(s.realized_pnl for s in wins) if wins else 0.0
    gross_loss = abs(sum(s.realized_pnl for s in losses)) if losses else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    sorted_sessions = sorted(sessions, key=lambda s: s.closed_at)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for s in sorted_sessions:
        cumulative += s.realized_pnl or 0
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    planned_rrs = []
    for s in sessions:
        sl_dist = abs(s.entry_price - s.stop_loss)
        tp_dist = abs(s.take_profit - s.entry_price)
        if sl_dist > 0:
            planned_rrs.append(tp_dist / sl_dist)
    avg_planned_rr = sum(planned_rrs) / len(planned_rrs) if planned_rrs else 0.0

    lines.append(f"- 期間: 過去 {period_days} 日")
    lines.append(f"- 総トレード数: {n}")
    lines.append(f"- 勝ち / 負け: {len(wins)} / {len(losses)}")
    lines.append(f"- 勝率: {win_rate * 100:.1f}%")
    lines.append(f"- Profit factor: {pf:.2f}" if pf != float("inf") else "- Profit factor: ∞")
    lines.append(f"- 平均 planned R:R: {avg_planned_rr:.2f}")
    lines.append(f"- 総損益: {total_pnl:+,.0f}")
    lines.append(f"- Max drawdown: -{max_dd:,.0f}")

    if n < _FEW_TRADES_THRESHOLD:
        lines.insert(2, f"⚠️ 統計的に不安定 (N={n}、推奨最低 {_FEW_TRADES_THRESHOLD} 件)")
        lines.insert(3, "")

    return "\n".join(lines)
