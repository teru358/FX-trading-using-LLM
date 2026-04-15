"""Audit オーケストレーター。

CLI (`audit` / `audit review`) から呼ばれる入り口。
session_store と price_store からデータを取得し、post_hoc 計算、レポート生成、
review mode 時は LLM 候補生成 + 対話選別を行う (review mode は Task 20)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.analysis.audit_lesson_generator import generate_candidates
from src.analysis.audit_post_hoc import (
    LessonCandidate,
    assign_flag,
    build_atr_pct_distribution,
    compute_counterfactuals,
    compute_mfe_mae,
    compute_vol_percentile,
)
from src.analysis.audit_report import (
    render_section1_summary,
    render_section2_calibration,
    render_section3_vol_regime,
    render_section4_time_of_day,
    render_section5_trade_table,
    render_section6_detailed_review,
    select_representative_trades,
)
from src.analysis.audit_reviewer import interactive_review
from src.data.session_store import SessionStore
from src.llm.factory import create_llm_client

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    report_path: Path
    session_count: int
    flag_counts: dict[str, int] = field(default_factory=dict)
    lessons_added: int = 0


_price_store_singleton = None


def _default_input_reader(prompt: str = "") -> str:
    """標準 stdin からの read ラッパー (テストで mock 可能)。"""
    return input(prompt)


def _load_pair_ohlcv(pair: str, since: datetime, until: datetime | None = None):
    """price_store から指定ペアの OHLCV を取得する。

    テスト時は monkeypatch でこの関数を差し替え可能。
    """
    store = _price_store_singleton
    if store is None:
        return None
    end = until or datetime.now()
    try:
        return store.load_ohlcv(pair, start=since, end=end)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[AUDIT] load_ohlcv failed for {pair}: {e}")
        return None


def run_audit(
    config: Any,
    days: int = 30,
    review: bool = False,
) -> AuditResult:
    """audit を実行し markdown レポートを生成する。

    Args:
        config: AppConfig (prices_db_path, audit_output_dir, tradeable_instruments 参照)
        days: 何日前までのトレードを対象にするか
        review: True なら対話 review モード (Task 20 で実装)
    """
    global _price_store_singleton
    from src.data.price_store import PriceStore

    # 出力ディレクトリを確保
    config.audit_output_dir.mkdir(parents=True, exist_ok=True)

    _price_store_singleton = PriceStore(config.prices_db_path)
    session_store = SessionStore(config.prices_db_path)

    now = datetime.now()
    since = now - timedelta(days=days)
    sessions = session_store.get_closed_sessions(since, now)
    logger.info(f"[AUDIT] Loaded {len(sessions)} closed sessions in last {days}d")

    if not sessions:
        report_path = _write_empty_report(config, days, now)
        return AuditResult(report_path=report_path, session_count=0)

    # post_hoc 計算
    pair_distributions: dict[str, list[float]] = {}
    for inst in config.tradeable_instruments:
        pair = inst.symbol
        ohlcv = _load_pair_ohlcv(pair, since=now - timedelta(days=90), until=now)
        pair_distributions[pair] = build_atr_pct_distribution(ohlcv)

    review_map: dict[str, dict[str, Any]] = {}
    flags_map: dict[str, str] = {}
    vol_percentile_map: dict[str, float | None] = {}

    for s in sessions:
        ohlcv = _load_pair_ohlcv(
            s.pair,
            since=s.opened_at - timedelta(hours=1),
            until=s.closed_at + timedelta(hours=48),
        )
        ph = compute_mfe_mae(
            direction=s.direction,
            entry_price=s.entry_price,
            close_price=s.close_price or s.entry_price,
            position_size=s.position_size or 0,
            opened_at=s.opened_at,
            closed_at=s.closed_at,
            ohlcv_df=ohlcv,
        )
        cf = compute_counterfactuals(
            direction=s.direction,
            entry_price=s.entry_price,
            stop_loss=s.stop_loss or s.entry_price,
            take_profit=s.take_profit or s.entry_price,
            position_size=s.position_size or 0,
            atr_value=s.atr_value or 0.01,
            opened_at=s.opened_at,
            closed_at=s.closed_at,
            ohlcv_df=ohlcv,
        )
        atr_pct = (s.atr_value or 0) / s.entry_price if s.entry_price else 0
        vol_pct = compute_vol_percentile(atr_pct, pair_distributions.get(s.pair, []))
        vol_percentile_map[s.session_id] = vol_pct

        flag = assign_flag(
            realized_pnl=s.realized_pnl or 0,
            signal_confidence=s.signal_confidence or 0,
            close_reason=s.close_reason or "",
            ph=ph, cf=cf,
        )
        flags_map[s.session_id] = flag

        review_map[s.session_id] = {
            "flag": flag,
            "mfe_during": ph.mfe_during_trade,
            "mae_during": ph.mae_during_trade,
            "mfe_after_close": ph.mfe_after_close_24h,
            "mae_after_close": ph.mae_after_close_24h,
            "tp_plus_0_5_atr_pnl": cf.tp_plus_0_5_atr_pnl,
            "tp_plus_1_0_atr_pnl": cf.tp_plus_1_0_atr_pnl,
            "sl_minus_0_5_atr_pnl": cf.sl_minus_0_5_atr_pnl,
            "tighter_sl_would_recover": cf.tighter_sl_would_recover,
            "vol_percentile": vol_pct,
        }

    # 選定
    selected = select_representative_trades(sessions, flags_map)

    lessons_added = 0
    accepted_lessons_map: dict[str, list] = {}

    if review:
        llm_client = create_llm_client(config, "reflection")
        audit_lessons_path = config.audit_lessons_path

        # 対象トレードに対する候補 eager 生成 + キャッシュ
        review_items = []
        for s in selected:
            ohlcv_for_llm = _load_pair_ohlcv(
                s.pair,
                since=s.opened_at - timedelta(hours=1),
                until=s.closed_at + timedelta(hours=48),
            )
            ph = compute_mfe_mae(
                direction=s.direction,
                entry_price=s.entry_price,
                close_price=s.close_price or s.entry_price,
                position_size=s.position_size or 0,
                opened_at=s.opened_at,
                closed_at=s.closed_at,
                ohlcv_df=ohlcv_for_llm,
            )
            cf = compute_counterfactuals(
                direction=s.direction,
                entry_price=s.entry_price,
                stop_loss=s.stop_loss or s.entry_price,
                take_profit=s.take_profit or s.entry_price,
                position_size=s.position_size or 0,
                atr_value=s.atr_value or 0.01,
                opened_at=s.opened_at,
                closed_at=s.closed_at,
                ohlcv_df=ohlcv_for_llm,
            )

            # LOSS のみ候補生成 (Phase 1 スコープ)
            if (s.realized_pnl or 0) >= 0:
                candidates = []
            else:
                cached = session_store.get_post_hoc_cache(s.session_id)
                if cached:
                    try:
                        cached_data = json.loads(cached)
                        candidates = [
                            LessonCandidate(**c) for c in cached_data.get("candidates", [])
                        ]
                    except Exception:
                        candidates = []
                else:
                    candidates = asyncio.run(generate_candidates(
                        session=s,
                        post_hoc=ph,
                        counterfactuals=cf,
                        vol_percentile=vol_percentile_map.get(s.session_id),
                        llm=llm_client,
                        hint="",
                    ))
                    # キャッシュ保存 (rule_text, rationale, applicability のみ)
                    session_store.set_post_hoc_cache(
                        s.session_id,
                        json.dumps({
                            "candidates": [
                                {
                                    "rule_text": c.rule_text,
                                    "rationale": c.rationale,
                                    "applicability": c.applicability,
                                }
                                for c in candidates
                            ],
                        }),
                    )

            review_items.append({
                "session": s,
                "post_hoc": ph,
                "counterfactuals": cf,
                "vol_percentile": vol_percentile_map.get(s.session_id),
                "candidates": candidates,
                "flag": flags_map[s.session_id],
            })

        async def _regen_wrapper(session, hint=""):
            match = next(
                (x for x in review_items if x["session"].session_id == session.session_id),
                None,
            )
            if match is None:
                return []
            return await generate_candidates(
                session=session,
                post_hoc=match["post_hoc"],
                counterfactuals=match["counterfactuals"],
                vol_percentile=match["vol_percentile"],
                llm=llm_client,
                hint=hint,
            )

        lessons_added = asyncio.run(interactive_review(
            items=review_items,
            lessons_path=audit_lessons_path,
            input_reader=_default_input_reader,
            regen_fn=_regen_wrapper,
        ))

    # markdown 生成
    parts = [
        f"# Performance Audit — {now.strftime('%Y-%m-%d')}",
        "",
        f"**Period**: {since.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} ({days} days)",
        f"**Generated**: {now.strftime('%Y-%m-%d %H:%M JST')}",
        "",
        "---",
        "",
        render_section1_summary(sessions, days),
        "",
        render_section2_calibration(sessions),
        "",
        render_section3_vol_regime(sessions, vol_percentile_map),
        "",
        render_section4_time_of_day(sessions),
        "",
        render_section5_trade_table(sessions, flags_map),
        "",
        render_section6_detailed_review(selected, review_map, accepted_lessons_map),
        "",
    ]

    report_path = config.audit_output_dir / f"{now.strftime('%Y-%m-%d')}-performance.md"
    if report_path.exists():
        report_path = config.audit_output_dir / f"{now.strftime('%Y-%m-%d-%H%M%S')}-performance.md"
    report_path.write_text("\n".join(parts), encoding="utf-8")
    logger.info(f"[AUDIT] Report written: {report_path}")

    flag_counts: dict[str, int] = {}
    for f in flags_map.values():
        flag_counts[f] = flag_counts.get(f, 0) + 1

    return AuditResult(
        report_path=report_path,
        session_count=len(sessions),
        flag_counts=flag_counts,
        lessons_added=lessons_added,
    )


def _write_empty_report(config: Any, days: int, now: datetime) -> Path:
    """該当期間にトレードがない場合のプレースホルダ。"""
    report_path = config.audit_output_dir / f"{now.strftime('%Y-%m-%d')}-performance.md"
    report_path.write_text(
        f"# Performance Audit — {now.strftime('%Y-%m-%d')}\n\n"
        f"過去 {days} 日間にクローズ済みトレードはありませんでした。\n",
        encoding="utf-8",
    )
    return report_path
