"""RiskGateWorker (Task 2.3) — deterministic hard veto。

design §5.2 Step5 / §5.3。PlannerAgent が accept した ExecutionPlanDraft を
最終的に決定論で検証する。vote ではなく hard veto: final_score が高くても
1 条件違反で否決する (§13#7)。

reject は 2 種類に分類する:
- structural (B): halt / bridge unhealthy / market closed / cooldown /
  stale required data。再起案しても直らない構造的問題 → pipeline は再起案しない。
- fixable (A): SL/TP の side, RR < min, spread 過大, SL/TP 欠落。
  ExecutionOpinionAgent が 1 回だけ再起案して直せる可能性がある。

structural を fixable より優先する (両方該当時は structural)。
advice_memo は不可侵 — risk gate は読まない・変更しない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.orchestrator.schemas import ExecutionPlanDraft

STRUCTURAL = "structural"
FIXABLE = "fixable"


@dataclass
class RiskGateResult:
    passed: bool
    reject_class: str | None = None  # None | "structural" | "fixable"
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """record_decision(risk_gate_result=...) 用の JSON 安全 dict。"""
        return {
            "passed": self.passed,
            "reject_class": self.reject_class,
            "issues": list(self.issues),
        }


class RiskGateWorker:
    """ExecutionPlanDraft の決定論 risk チェック。

    Parameters
    ----------
    min_rr : 最低 reward/risk 比。draft.action["rr"] がこれ未満なら fixable reject。
    spread_max_pips : 許容 spread (pips)。quote spread がこれ超で fixable reject。
    pip_size : pips → price 換算 (USDJPY=0.01, EURUSD=0.0001)。
    """

    def __init__(self, *, min_rr: float = 1.5, spread_max_pips: float = 2.0, pip_size: float = 0.01) -> None:
        self._min_rr = min_rr
        self._spread_max_pips = spread_max_pips
        self._pip_size = pip_size

    def pre_check(self, draft: ExecutionPlanDraft, context: dict[str, Any]) -> RiskGateResult:
        """draft を context に対して検証する。

        structural を最優先で判定し、該当すれば即座に structural reject を返す
        (fixable と混在しても structural を返す = §5.3 の「構造的は再起案しない」)。
        """
        structural = self._structural_issues(context)
        if structural:
            return RiskGateResult(passed=False, reject_class=STRUCTURAL, issues=structural)

        fixable = self._fixable_issues(draft, context)
        if fixable:
            return RiskGateResult(passed=False, reject_class=FIXABLE, issues=fixable)

        return RiskGateResult(passed=True, reject_class=None, issues=[])

    # ── structural (B): 再起案不可 ──────────────────────────────

    def _structural_issues(self, context: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        risk = context.get("risk_state", {})
        if risk.get("halt", "none") != "none":
            issues.append(f"halt active: {risk.get('halt')}")
        if risk.get("bridge_health", "healthy") != "healthy":
            issues.append(f"bridge unhealthy: {risk.get('bridge_health')}")
        if not risk.get("market_open", True):
            issues.append("market closed")
        if risk.get("cooldown", False):
            issues.append("cooldown active")
        # stale required data: technical が ok 以外なら構造的 (新鮮な必須入力が無い)。
        tech_status = context.get("technical", {}).get("status", "missing")
        if tech_status != "ok":
            issues.append(f"required technical data stale: {tech_status}")
        return issues

    # ── fixable (A): ExecutionOpinion 再起案で直せる可能性 ────────

    def _fixable_issues(self, draft, context: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        action = draft.action
        mid = context.get("quote", {}).get("mid")

        sl = action.get("sl")
        tp = action.get("tp")
        if sl is None:
            issues.append("missing sl")
        if tp is None:
            issues.append("missing tp")

        # SL/TP の side 整合 (long: SL<entry<TP, short: TP<entry<SL)。
        if sl is not None and tp is not None and mid is not None:
            if draft.direction == "long":
                if sl >= mid:
                    issues.append("long sl must be below entry")
                if tp <= mid:
                    issues.append("long tp must be above entry")
            elif draft.direction == "short":
                if sl <= mid:
                    issues.append("short sl must be above entry")
                if tp >= mid:
                    issues.append("short tp must be below entry")

        # RR 下限。
        rr = action.get("rr")
        if rr is not None and rr < self._min_rr:
            issues.append(f"rr {rr} below min {self._min_rr}")

        # spread 上限。
        spread = context.get("quote", {}).get("spread")
        if spread is not None and self._pip_size > 0:
            spread_pips = spread / self._pip_size
            if spread_pips > self._spread_max_pips:
                issues.append(
                    f"spread {spread_pips:.1f}pips above max {self._spread_max_pips}"
                )

        return issues
