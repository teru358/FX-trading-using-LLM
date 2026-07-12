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


def render_section2_calibration(sessions: list) -> str:
    """Section 2: confidence 帯ごとの勝率分布。"""
    lines = ["## Section 2: Confidence Calibration", ""]
    if len(sessions) < _FEW_TRADES_THRESHOLD:
        lines.append(f"トレード数が少なく (N={len(sessions)})、calibration は省略します。")
        return "\n".join(lines)

    buckets: dict[tuple[float, float], list] = {}
    bounds = [(0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 1.00)]
    for lo, hi in bounds:
        buckets[(lo, hi)] = []

    for s in sessions:
        conf = s.signal_confidence or 0
        for lo, hi in bounds:
            if lo <= conf < hi or (hi == 1.00 and conf >= hi):
                buckets[(lo, hi)].append(s)
                break

    lines.append("| conf 帯 | count | win rate | avg pnl |")
    lines.append("|---|---|---|---|")
    for (lo, hi), sess_list in buckets.items():
        if not sess_list:
            lines.append(f"| {lo:.2f}-{hi:.2f} | 0 | — | — |")
            continue
        wins = sum(1 for x in sess_list if (x.realized_pnl or 0) > 0)
        count = len(sess_list)
        wr = wins / count * 100
        avg_pnl = sum(x.realized_pnl or 0 for x in sess_list) / count
        lines.append(f"| {lo:.2f}-{hi:.2f} | {count} | {wr:.1f}% | {avg_pnl:+,.0f} |")

    return "\n".join(lines)


def render_section3_vol_regime(sessions: list, vol_percentile_map: dict[str, float | None]) -> str:
    """Section 3: ペア別 90 日分布で割った ボラレジーム breakdown。"""
    lines = ["## Section 3: ボラレジーム Breakdown", ""]
    if len(sessions) < _FEW_TRADES_THRESHOLD:
        lines.append(f"トレード数が少なく (N={len(sessions)})、regime 分析は省略します。")
        return "\n".join(lines)

    buckets = {
        "0-20 (低ボラ)": [],
        "20-40": [],
        "40-60": [],
        "60-80": [],
        "80-100 (高ボラ)": [],
    }
    excluded = 0
    for s in sessions:
        pct = vol_percentile_map.get(s.session_id)
        if pct is None:
            excluded += 1
            continue
        if pct < 20:
            buckets["0-20 (低ボラ)"].append(s)
        elif pct < 40:
            buckets["20-40"].append(s)
        elif pct < 60:
            buckets["40-60"].append(s)
        elif pct < 80:
            buckets["60-80"].append(s)
        else:
            buckets["80-100 (高ボラ)"].append(s)

    lines.append("| ボラ percentile | count | win rate | avg pnl |")
    lines.append("|---|---|---|---|")
    for label, sess_list in buckets.items():
        if not sess_list:
            lines.append(f"| {label} | 0 | — | — |")
            continue
        count = len(sess_list)
        wins = sum(1 for x in sess_list if (x.realized_pnl or 0) > 0)
        wr = wins / count * 100
        avg_pnl = sum(x.realized_pnl or 0 for x in sess_list) / count
        lines.append(f"| {label} | {count} | {wr:.1f}% | {avg_pnl:+,.0f} |")

    if excluded:
        lines.append("")
        lines.append(f"_{excluded} 件は OHLCV データ不足のため除外_")

    return "\n".join(lines)


def render_section4_time_of_day(sessions: list) -> str:
    """Section 4: エントリー時刻 (JST hour) のバケット breakdown。"""
    lines = ["## Section 4: 時間帯 (JST hour)", ""]
    if len(sessions) < _FEW_TRADES_THRESHOLD:
        lines.append(f"トレード数が少なく (N={len(sessions)})、時間帯分析は省略します。")
        return "\n".join(lines)

    by_hour: dict[int, list] = {}
    for s in sessions:
        h = s.opened_at.hour
        by_hour.setdefault(h, []).append(s)

    lines.append("| hour | count | win rate | avg pnl |")
    lines.append("|---|---|---|---|")
    for h in sorted(by_hour.keys()):
        sess_list = by_hour[h]
        count = len(sess_list)
        if count < 2:
            continue  # 1 件だけの時間帯はノイズなので除外
        wins = sum(1 for x in sess_list if (x.realized_pnl or 0) > 0)
        wr = wins / count * 100
        avg_pnl = sum(x.realized_pnl or 0 for x in sess_list) / count
        lines.append(f"| {h:02d} | {count} | {wr:.1f}% | {avg_pnl:+,.0f} |")

    return "\n".join(lines)


def render_section5_trade_table(sessions: list, flags_map: dict[str, str]) -> str:
    """Section 5: 全トレード一覧テーブル。"""
    lines = ["## Section 5: 全トレードテーブル", ""]
    if not sessions:
        lines.append("トレードなし。")
        return "\n".join(lines)

    lines.append("| # | date | pair | dir | conf | pnl | close_reason | flag |")
    lines.append("|---|---|---|---|---|---|---|---|")

    sorted_s = sorted(sessions, key=lambda s: s.closed_at)
    for i, s in enumerate(sorted_s, start=1):
        date_str = s.closed_at.strftime("%m-%d %H:%M")
        flag = flags_map.get(s.session_id, "?")
        pnl = s.realized_pnl or 0
        conf = s.signal_confidence or 0
        lines.append(
            f"| {i} | {date_str} | {s.pair} | {s.direction.upper()} | "
            f"{conf:.2f} | {pnl:+,.0f} | {s.close_reason or '-'} | {flag} |"
        )

    return "\n".join(lines)


def select_representative_trades(
    sessions: list,
    flags_map: dict[str, str],
    max_wins: int = 5,
    max_losses: int = 5,
) -> list:
    """Section 6 で深掘りする代表 10 件を選定する。

    戦略:
    - 非 NOISE / 非 INSUFFICIENT_DATA を優先
    - 勝ち: pnl 降順で max_wins 件 (TIGHT_TP / LATE_EXIT 優先)
    - 敗:   pnl 昇順で max_losses 件 (CONF_MISS / SL_RECOVER 優先)
    - 上記で枠が埋まらなければ NOISE/INSUFFICIENT_DATA から |pnl| 順に補充
      (全トレードが小粒でも必ず代表トレードをレビューに含めるため)
    """
    excluded_flags = {"NOISE", "INSUFFICIENT_DATA"}
    candidates = [s for s in sessions if flags_map.get(s.session_id) not in excluded_flags]

    wins = [s for s in candidates if (s.realized_pnl or 0) > 0]
    losses = [s for s in candidates if (s.realized_pnl or 0) <= 0]

    priority_win_flags = {"TIGHT_TP", "LATE_EXIT"}
    wins.sort(
        key=lambda s: (
            0 if flags_map.get(s.session_id) in priority_win_flags else 1,
            -(s.realized_pnl or 0),
        )
    )
    selected_wins = wins[:max_wins]

    priority_loss_flags = {"CONF_MISS", "SL_RECOVER"}
    losses.sort(
        key=lambda s: (
            0 if flags_map.get(s.session_id) in priority_loss_flags else 1,
            s.realized_pnl or 0,
        )
    )
    selected_losses = losses[:max_losses]

    need_wins = max_wins - len(selected_wins)
    need_losses = max_losses - len(selected_losses)
    if need_wins > 0 or need_losses > 0:
        already = {s.session_id for s in selected_wins + selected_losses}
        fallback_pool = [
            s for s in sessions
            if s.session_id not in already
            and flags_map.get(s.session_id) in excluded_flags
        ]
        if need_wins > 0:
            fb_wins = sorted(
                (s for s in fallback_pool if (s.realized_pnl or 0) > 0),
                key=lambda s: -(s.realized_pnl or 0),
            )
            selected_wins.extend(fb_wins[:need_wins])
        if need_losses > 0:
            fb_losses = sorted(
                (s for s in fallback_pool if (s.realized_pnl or 0) <= 0),
                key=lambda s: (s.realized_pnl or 0),
            )
            selected_losses.extend(fb_losses[:need_losses])

    return selected_wins + selected_losses


def render_section6_detailed_review(
    selected: list,
    review_map: dict[str, dict[str, Any]],
) -> str:
    """Section 6: 代表 10 件の詳細レビュー。

    review_map: session_id → {flag, mfe_*, mae_*, counterfactual, ...}
    """
    lines = ["## Section 6: 詳細レビュー (代表 10 件)", ""]
    if not selected:
        lines.append("レビュー対象なし。")
        return "\n".join(lines)

    for s in selected:
        data = review_map.get(s.session_id, {})
        flag = data.get("flag", "?")
        pnl_val = s.realized_pnl or 0
        close_px = s.close_price or 0.0
        conf_val = s.signal_confidence or 0.0
        is_win = pnl_val > 0
        label = "WIN" if is_win else "LOSS"

        lines.append(f"### [{label}] {s.closed_at.strftime('%Y-%m-%d %H:%M')} {s.pair} "
                     f"{s.direction.upper()} conf={conf_val:.2f} "
                     f"pnl={pnl_val:+,.0f} flag={flag}")
        lines.append("")
        lines.append("**エントリー時分析**:")
        lines.append(f"> {s.analysis_summary or '(none)'}")
        lines.append("")
        lines.append(f"**Macro at entry**: {s.macro_context or '(none)'}")
        lines.append("")
        lines.append("**Post-hoc data**:")
        lines.append(f"- Close: {close_px:.5f} ({s.close_reason})")
        duration_h = (s.closed_at - s.opened_at).total_seconds() / 3600
        lines.append(f"- Duration: {duration_h:.1f}h")
        lines.append(f"- MFE during trade: {data.get('mfe_during', 0):+,.0f}")
        lines.append(f"- MAE during trade: {data.get('mae_during', 0):+,.0f}")
        lines.append(f"- MFE after close 24h: {data.get('mfe_after_close', 0):+,.0f}")
        lines.append(f"- MAE after close 24h: {data.get('mae_after_close', 0):+,.0f}")
        lines.append(f"- Counterfactual TP +0.5 ATR pnl: {data.get('tp_plus_0_5_atr_pnl', 0):+,.0f}")
        lines.append(f"- Counterfactual TP +1.0 ATR pnl: {data.get('tp_plus_1_0_atr_pnl', 0):+,.0f}")
        lines.append(f"- Counterfactual SL -0.5 ATR pnl: {data.get('sl_minus_0_5_atr_pnl', 0):+,.0f}")
        lines.append("")
        if s.reflection_text:
            lines.append("**Reflection (決済時自動生成)**:")
            lines.append(f"> {s.reflection_text}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)
