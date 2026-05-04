"""週次自己診断レポートジョブ。

FX 休場 (土日) に schedule ライブラリから呼び出し、過去 N 日のパフォーマンス、
予測精度、LLM 使用量、現状設定を集約して Claude に診断レポートを生成させる。
出力は Markdown ファイル + Discord embed (オプション)。

cron に依存せず、main.py の schedule.every().<weekday>.at(time) で起動する。
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.analysis.prompt_loader import load_prompt, render_prompt
from src.config import BASE_DIR, AppConfig
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.signals.accuracy_tracker import compute_recent_accuracy
from src.trading.position_manager import PositionManager
from src.utils.clock import db_now, local_now

logger = logging.getLogger(__name__)


# ── データ収集ヘルパー ─────────────────────────────────────────────


def _build_performance_section(
    config: AppConfig, lookback_days: int,
) -> str:
    """直近 N 日のクローズトレードからサマリを作る。"""
    state_store = StateStore(config.state_dir)
    pm = PositionManager(state_store, context="WeeklyDiag")
    account = pm.get_account_state()

    cutoff = db_now() - timedelta(days=lookback_days)
    recent = [
        t for t in account.closed_trades
        if t.closed_at and t.closed_at >= cutoff
    ]
    if not recent:
        return f"クローズトレードなし (過去 {lookback_days} 日)。"

    wins = [t for t in recent if (t.realized_pnl or 0) > 0]
    losses = [t for t in recent if (t.realized_pnl or 0) < 0]
    total_pnl = sum(t.realized_pnl or 0 for t in recent)
    gross_profit = sum(t.realized_pnl for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.realized_pnl for t in losses)) if losses else 0.0
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )

    by_pair: dict[str, list] = {}
    for t in recent:
        by_pair.setdefault(t.pair, []).append(t)

    by_reason: Counter[str] = Counter(
        t.close_reason or "unknown" for t in recent
    )

    lines = [
        f"- 期間: 直近 {lookback_days} 日",
        f"- トレード数: {len(recent)} (勝 {len(wins)} / 負 {len(losses)})",
        f"- 勝率: {len(wins)/len(recent)*100:.1f}%",
        f"- 合計 P&L: {total_pnl:+,.2f}",
        f"- Profit Factor: {profit_factor:.2f}",
        f"- 残高: {account.balance:,.2f} (累計 {account.balance - account.initial_balance:+,.2f})",
        "",
        "**ペア別**:",
    ]
    for pair, trades in sorted(by_pair.items()):
        p_wins = sum(1 for t in trades if (t.realized_pnl or 0) > 0)
        p_pnl = sum(t.realized_pnl or 0 for t in trades)
        lines.append(
            f"- {pair}: {len(trades)} 件 "
            f"(勝率 {p_wins/len(trades)*100:.0f}%, P&L {p_pnl:+,.2f})"
        )
    lines.append("")
    lines.append("**決済理由内訳**:")
    for reason, n in by_reason.most_common():
        lines.append(f"- {reason}: {n}")
    return "\n".join(lines)


def _build_accuracy_section(
    config: AppConfig, forecast_store, lookback_days: int,
) -> str:
    """ペア別の forecast accuracy 集計。"""
    pairs = (
        config.tradeable_instruments + config.watch_only_instruments
    )
    hours = lookback_days * 24
    lines: list[str] = []
    for inst in pairs:
        result = compute_recent_accuracy(forecast_store, inst.symbol, hours=hours)
        if result is None:
            lines.append(f"- {inst.display_name} ({inst.symbol}): データ不足")
            continue
        marker = ""
        if result.accuracy < 0.33:
            marker = "  ⚠ hard_threshold (33%) 未満"
        elif result.accuracy < 0.50:
            marker = "  ⚠ soft_threshold (50%) 未満"
        lines.append(
            f"- {inst.display_name} ({inst.symbol}): "
            f"{result.correct_count}/{result.sample_count} = {result.accuracy:.0%}"
            f"{marker}"
        )
    return "\n".join(lines) if lines else "予測データなし。"


def _build_llm_status_section() -> str:
    """LLM プロバイダの CB スナップショット。"""
    try:
        from src.llm.client import _circuit_breakers
    except Exception as e:  # noqa: BLE001
        return f"(取得失敗: {e})"

    if not _circuit_breakers:
        return "LLM 呼び出し未発生 (起動直後の可能性)。"

    lines: list[str] = []
    for provider, cb in _circuit_breakers.items():
        snap = cb.snapshot()
        line = (
            f"- {provider}: state={snap['state']} "
            f"success={snap['total_success']} "
            f"fail={snap['total_failure']} "
            f"usage_limit_hits={snap['usage_limit_hits']}"
        )
        last = snap.get("last_error")
        if last:
            line += f" | last_error: {last['type']} — {last['message'][:80]}"
        lines.append(line)
    return "\n".join(lines)


def _build_existing_features_section(config: AppConfig) -> str:
    """LLM が既存機能を再発明しないようにサマリを渡す。"""
    fa = config.trading.forecast_accuracy_feedback
    items = [
        "- conflict_penalty: news×price 方向矛盾時 confidence × (1 - 0.3 × min_conf)",
        "- market_regime deadband: trending → ×0.8, ranging → ×1.2",
        f"- forecast_accuracy_feedback: {'有効' if fa.enabled else '無効'} "
        f"(soft={fa.soft_threshold}, hard={fa.hard_threshold}, "
        f"penalty=×{fa.confidence_penalty})",
        "- circuit_breaker: 連続失敗 3 回で 300s OPEN、半開で再試行",
        "- TV consensus 矛盾検出: TV 方向と price 方向が逆なら conf ×0.7",
        "- drawdown_kill_switch: peak から閾値以上落ちたら新規 entry 停止",
        "- vol_regime: ATR/EWMA に基づく risk_per_trade スケーリング",
        "- audit_lesson_generator: クローズトレードから RAG に教訓蓄積",
        "- reflection cycle: クローズトレード振り返りを insights collection に保存",
    ]
    return "\n".join(items)


def _build_settings_section(config: AppConfig) -> str:
    t = config.trading
    fa = t.forecast_accuracy_feedback
    lines = [
        f"- news_weight: {t.news_weight}",
        f"- price_weight: {t.price_weight}",
        f"- signal_deadband: {t.signal_deadband}",
        f"- signal_confidence_threshold: {t.signal_confidence_threshold}",
        f"- risk_per_trade: {t.risk_per_trade}",
        f"- min_rr_ratio: {t.min_rr_ratio}",
        f"- forecast_accuracy_feedback: enabled={fa.enabled}, "
        f"soft={fa.soft_threshold}, hard={fa.hard_threshold}, "
        f"min_samples={fa.min_samples}, penalty=×{fa.confidence_penalty}",
        f"- drawdown_kill_switch_enabled: {t.drawdown_kill_switch_enabled}",
        f"- vol_regime_enabled: {t.vol_regime_enabled}",
    ]
    return "\n".join(lines)


# ── メインジョブ ──────────────────────────────────────────────────


async def _run_diagnosis_async(
    config: AppConfig,
    forecast_store,
) -> tuple[str, Path]:
    """LLM を呼んで診断レポートを生成し、ファイルに保存する。

    Returns:
        (report_text, report_path)
    """
    cfg = config.weekly_diagnosis
    now = local_now(config)
    period_end = now
    period_start = now - timedelta(days=cfg.lookback_days)

    user_prompt = render_prompt(
        "weekly_diagnosis_user.j2",
        period_start=period_start.strftime("%Y-%m-%d %H:%M %Z"),
        period_end=period_end.strftime("%Y-%m-%d %H:%M %Z"),
        lookback_days=cfg.lookback_days,
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        performance_section=_build_performance_section(config, cfg.lookback_days),
        accuracy_section=_build_accuracy_section(config, forecast_store, cfg.lookback_days),
        llm_status_section=_build_llm_status_section(),
        existing_features_section=_build_existing_features_section(config),
        settings_section=_build_settings_section(config),
    )

    llm = create_llm_client(config, cfg.llm_role)
    logger.info(
        f"[WEEKLY] Calling LLM ({llm.provider_name}/{llm.model_name}, "
        f"prompt={len(user_prompt)} chars)..."
    )
    messages = [
        {"role": "system", "content": load_prompt("weekly_diagnosis_system.txt")},
        {"role": "user", "content": user_prompt},
    ]
    report = await llm.chat(messages, temperature=0.3)
    report = report.strip()

    # ── 保存 ──
    out_dir = BASE_DIR / cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"diagnosis-{now.strftime('%Y-%m-%d')}.md"
    out_path = out_dir / fname
    header = (
        f"# 週次診断レポート\n\n"
        f"- 期間: {period_start.strftime('%Y-%m-%d')} 〜 {period_end.strftime('%Y-%m-%d')}\n"
        f"- 生成: {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"- LLM: {llm.provider_name} / {llm.model_name}\n\n"
        f"---\n\n"
    )
    out_path.write_text(header + report, encoding="utf-8")
    logger.info(f"[WEEKLY] Report saved: {out_path}")
    return report, out_path


def _send_discord_embed(
    report: str, report_path: Path, config: AppConfig,
) -> None:
    """Discord に embed で送信 (best effort)。"""
    if not config.weekly_diagnosis.notify_discord:
        return
    try:
        # サマリ部分のみ embed description に入れる (4096 文字上限)
        # report 先頭から ## サマリ ブロックを抜き出す。なければ先頭 1500 文字。
        summary = _extract_summary_block(report) or report[:1500]
        if len(summary) > 3800:
            summary = summary[:3800] + "\n…(続きはレポートファイル参照)"

        from src.api.notifications import send_discord_embed
        send_discord_embed(
            title=f"📊 週次診断レポート ({db_now().strftime('%Y-%m-%d')})",
            description=summary,
            color=0x3498db,
            footer=f"file: {report_path.name}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WEEKLY] Discord notify failed: {e}")


def _extract_summary_block(report: str) -> str | None:
    """Markdown の `## サマリ` 〜 次の `## ` までを抜き出す。"""
    import re
    m = re.search(r"##\s*サマリ\b(.*?)(?=\n##\s|\Z)", report, re.DOTALL)
    if not m:
        return None
    return ("## サマリ" + m.group(1)).strip()


def run_weekly_diagnosis(
    config: AppConfig,
    forecast_store,
) -> None:
    """schedule ライブラリから呼ばれる同期エントリポイント。

    enabled=False ならノーオペで終了。失敗してもデーモンは止まらない。
    """
    if not config.weekly_diagnosis.enabled:
        logger.debug("[WEEKLY] Disabled, skipping.")
        return

    try:
        report, path = asyncio.run(_run_diagnosis_async(config, forecast_store))
        _send_discord_embed(report, path, config)
        logger.info(f"[WEEKLY] Diagnosis complete: {path}")
    except Exception as e:  # noqa: BLE001 - デーモン停止防止
        logger.error(f"[WEEKLY] Diagnosis failed: {e}", exc_info=True)
