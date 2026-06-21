"""WatchEvaluator — watch loop の決定論評価層 (design §6.2 / §6.3 / §7.2)。

active plan を live quote / technical / risk_state に対して評価する純粋ロジック。
DB / broker に触れず、runtime がこれを合成して shadow trigger flow を組む。

評価対象:
- entry_conditions: §6.2 vocabulary を **AND** で評価 (or / nested は禁止)。
- invalidation:     §6.3 vocabulary + expires_at 超過 (expired marker 無しでも expired)。
- freshness wall:   §7.2 trigger 直前の最終検証 (quote/technical/spread/halt/market)。

context dict の前提キー (runtime が組む):
    pair, quote{mid, spread}, technical{status, age_sec},
    risk_state{halt, market_open}, news_conflict(bool), quote_age_sec(float)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.config.schema import OrchestratorEntryConfig
from src.orchestrator.schemas import EntryCondition, InvalidationCondition


def _pip_size_for(pair: str | None) -> float:
    """pair の pip size。JPY クロスは 0.01、それ以外は 0.0001 (risk_gate と同一規則)。"""
    if pair is None:
        return 0.01
    return 0.01 if "JPY" in pair.upper() else 0.0001


class WatchEvaluator:
    """entry / invalidation / freshness を決定論で評価する。

    Parameters
    ----------
    entry : OrchestratorEntryConfig
        freshness final wall の閾値 (spread_max_pips / max_quote_age_seconds /
        require_fresh_technical / max_technical_age_seconds) を持つ。
    """

    def __init__(self, entry: OrchestratorEntryConfig) -> None:
        self._entry = entry

    # ── entry_conditions (§6.2, AND only) ──────────────────────

    def entry_conditions_hold(
        self, conditions: list[EntryCondition], context: dict[str, Any]
    ) -> bool:
        """全 entry_condition が成立すれば True (AND)。空リストは False。

        空を False にするのは vacuous truth による trigger 暴発を防ぐため
        (条件無し plan を「常に成立」と解釈しない)。
        """
        if not conditions:
            return False
        return all(self._entry_one_holds(c, context) for c in conditions)

    def _entry_one_holds(self, cond: EntryCondition, context: dict[str, Any]) -> bool:
        mid = context.get("quote", {}).get("mid")
        if cond.type in ("price_at_or_below", "price_at_or_above",
                          "breakout_above", "breakout_below"):
            if mid is None or cond.value is None:
                return False
            if cond.type == "price_at_or_below":
                return mid <= cond.value
            if cond.type == "price_at_or_above":
                return mid >= cond.value
            if cond.type == "breakout_above":
                return mid > cond.value
            return mid < cond.value  # breakout_below
        if cond.type == "spread_below":
            spread = context.get("quote", {}).get("spread")
            if spread is None or cond.value_pips is None:
                return False
            pip = _pip_size_for(context.get("pair"))
            return (spread / pip) < cond.value_pips
        if cond.type == "technical_status_is":
            return context.get("technical", {}).get("status") == cond.status
        return False

    # ── invalidation (§6.3) + expiry ───────────────────────────

    def invalidation_reason(
        self,
        conditions: list[InvalidationCondition],
        context: dict[str, Any],
        *,
        now: datetime,
        expires_at: datetime | None,
    ) -> str | None:
        """成立した invalidation の type を返す。何も成立しなければ None。

        expires_at 超過は invalidation list に 'expired' marker が無くても
        "expired" を返す (時間失効は plan の暗黙不変条件)。expiry を最優先で見る。
        """
        if expires_at is not None and now >= expires_at:
            return "expired"
        mid = context.get("quote", {}).get("mid")
        for cond in conditions:
            if cond.type == "expired":
                # 明示 marker。expires_at 判定で拾えなかったケースの保険。
                if expires_at is not None and now >= expires_at:
                    return "expired"
            elif cond.type == "price_below":
                if mid is not None and cond.value is not None and mid < cond.value:
                    return "price_below"
            elif cond.type == "price_above":
                if mid is not None and cond.value is not None and mid > cond.value:
                    return "price_above"
            elif cond.type == "technical_stale":
                if context.get("technical", {}).get("status") != "ok":
                    return "technical_stale"
            elif cond.type == "news_conflict":
                if context.get("news_conflict", False):
                    return "news_conflict"
        return None

    # ── freshness final wall (§7.2) ────────────────────────────

    def freshness_issues(self, context: dict[str, Any]) -> list[str]:
        """trigger 直前の最終鮮度検証。問題があれば issue 文字列のリストを返す。

        空リスト = 通過 (trigger 可)。1 件でもあれば trigger しない (§7.2)。
        """
        issues: list[str] = []

        # age 不明 (None / 欠落) は安全側 = block。time parse 不能や aware/naive 混在で
        # _enrich_ages が age を出せないと None になる。これを「鮮度 OK」と誤認すると
        # tz 不整合時に古い quote で trigger しうる (過去に timezone 実害, Codex Medium)。
        quote_age = context.get("quote_age_sec")
        if quote_age is None:
            issues.append("quote age unknown (uncomputable)")
        elif quote_age > self._entry.max_quote_age_seconds:
            issues.append(
                f"quote stale: age {quote_age:.1f}s > {self._entry.max_quote_age_seconds}s"
            )

        if self._entry.require_fresh_technical:
            tech = context.get("technical", {})
            status = tech.get("status")
            if status != "ok":
                issues.append(f"technical not ok: {status}")
            tech_age = tech.get("age_sec")
            if tech_age is None:
                issues.append("technical age unknown (uncomputable)")
            elif tech_age > self._entry.max_technical_age_seconds:
                issues.append(
                    f"technical stale: age {tech_age:.0f}s "
                    f"> {self._entry.max_technical_age_seconds}s"
                )

        spread = context.get("quote", {}).get("spread")
        if spread is not None:
            pip = _pip_size_for(context.get("pair"))
            spread_pips = spread / pip
            if spread_pips > self._entry.spread_max_pips:
                issues.append(
                    f"spread {spread_pips:.1f}pips above max {self._entry.spread_max_pips}"
                )

        risk = context.get("risk_state", {})
        if risk.get("halt", "none") != "none":
            issues.append(f"halt active: {risk.get('halt')}")
        if not risk.get("market_open", True):
            issues.append("market closed")

        return issues
