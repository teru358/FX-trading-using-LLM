"""RiskGateWorker (Task 2.3) — deterministic hard veto。

design §5.2 Step5 / §5.3。PlannerAgent が accept した ExecutionPlanDraft を
最終的に決定論で検証する。vote ではなく hard veto: final_score が高くても
1 条件違反で否決する (§13#7)。

reject は 2 種類に分類する:
- structural (B): halt / bridge unhealthy / market closed / cooldown /
  stale required data。再起案しても直らない構造的問題 → pipeline は再起案しない。
- fixable (A): SL/TP の side, 導出 RR < min (申告 rr でなく derive_rr), spread 過大,
  SL/TP 欠落。
  ExecutionOpinionAgent が 1 回だけ再起案して直せる可能性がある。

structural を fixable より優先する (両方該当時は structural)。
advice_memo は不可侵 — risk gate は読まない・変更しない。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from src.orchestrator.schemas import ExecutionPlanDraft, _ENTRY_PRICE_TYPES

STRUCTURAL = "structural"
FIXABLE = "fixable"

# bridge health の健全値。context_builder は 'ok' を、一部 fixture は 'healthy' を使う。
_HEALTHY_BRIDGE = frozenset({"ok", "healthy"})


def _default_pip_size_for(pair: str) -> float:
    """pair の pip size。JPY を含むクロスは 0.01、それ以外は 0.0001。

    既存 symbol 表記 (USDJPY=X / EURUSD=X) を想定。'JPY' 部分一致で判定。
    """
    return 0.01 if "JPY" in pair.upper() else 0.0001


def _executable_price(direction: str, quote: dict[str, Any] | None) -> float | None:
    """約定想定価格: long は ask (買い)、short は bid (売り)。無ければ mid。

    quote は schema 層を通らない untrusted 入力 — 非数値 (str 等) は None に落とし
    導出不能側へ倒す (hard veto 関数を評価中に TypeError で殺さない)。
    0.0 は有効値としてそのまま返す (退化候補として _rr 側で rr=0.0 になる)。
    """
    if not quote:
        return None
    price = quote.get("ask") if direction == "long" else quote.get("bid")
    if price is None:
        price = quote.get("mid")
    if price is None:
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def derive_rr(
    draft: ExecutionPlanDraft,
    quote: dict[str, Any] | None,
    *,
    include_executable_price: bool,
) -> float | None:
    """sl/tp/entry 候補から reward/risk 比を決定的に導出する (spec 2026-07-16 §2.A)。

    entry 候補 = price 系 entry_condition の value 全部。実行価格 (long=ask /
    short=bid、無ければ mid) は include_executable_price=True のとき、または
    price 系候補ゼロのとき (underivable 回避 fallback) に追加する。

    phase 分離 (R2 High#1): planning/coerce は False — 押し目 plan の entry が
    現在価格から離れているのは正常で、約定は条件成立後 (watch_evaluator が評価)。
    live final gate / shadow precheck は True — breakout オーバーシュートの
    実行 RR 劣化を trigger 時価格で検出する。

    候補ごとに rr を計算し **最小値** を採用する (保守則 — SL に近い entry ほど
    rr は大きく出るため、min でしか最悪ケースを取れない)。退化候補
    (risk<=0 / reward<0) と非有限候補 (NaN/Inf — quote は schema 層を通らない,
    R2 High#3) は rr 0.0 として min に参加させる (黙って除外すると悪い候補ほど
    無視され、NaN は比較 False で偽 pass する)。

    None (導出不能): sl or tp 欠落 / entry 候補ゼロ / include_executable_price=True
    かつ実行価格取得不能 (欠落・非数値で trigger 時実勢を確認できない, R4 High#1)。
    """
    sl = draft.action.get("sl")
    tp = draft.action.get("tp")
    if sl is None or tp is None:
        return None

    candidates: list[float] = [
        c.value for c in draft.entry_conditions
        if c.type in _ENTRY_PRICE_TYPES and c.value is not None
    ]
    if include_executable_price:
        # live/shadow phase: 実勢価格で trigger 時 RR を検証するのが目的。
        # 実行価格が取れない (欠落/非数値) なら検証不能 → 導出不能に倒す (R4 High#1)。
        # price 条件候補 (計画 RR) だけで pass させない — breakout オーバーシュートを
        # 見逃す。「trigger 時に実勢を確認できないなら発注しない」(spread unknown と同思想)。
        executable = _executable_price(draft.direction, quote)
        if executable is None:
            return None
        candidates.append(executable)
    elif not candidates:
        # planning phase で price 条件ゼロのときのみ実行価格に fallback (underivable 回避)。
        executable = _executable_price(draft.direction, quote)
        if executable is not None:
            candidates.append(executable)
    if not candidates:
        return None

    def _rr(entry: float) -> float:
        if not (math.isfinite(entry) and math.isfinite(sl) and math.isfinite(tp)):
            return 0.0  # 非有限は reject 方向 (深層防御)
        if draft.direction == "long":
            reward, risk = tp - entry, entry - sl
        else:
            reward, risk = entry - tp, sl - entry
        if risk <= 0 or reward < 0:
            return 0.0
        return reward / risk

    return min(_rr(e) for e in candidates)


@dataclass
class RiskGateResult:
    passed: bool
    reject_class: str | None = None  # None | "structural" | "fixable"
    issues: list[str] = field(default_factory=list)
    derived_rr: float | None = None  # RR 導出済み経路のみ値・structural/導出前は None

    def to_dict(self) -> dict[str, Any]:
        """record_decision(risk_gate_result=...) 用の JSON 安全 dict。"""
        return {
            "passed": self.passed,
            "reject_class": self.reject_class,
            "issues": list(self.issues),
            "derived_rr": self.derived_rr,
        }


class RiskGateWorker:
    """ExecutionPlanDraft の決定論 risk チェック。

    Parameters
    ----------
    min_rr : 最低 reward/risk 比。derive_rr の導出 RR (申告 action["rr"] は不使用)
        がこれ未満なら fixable reject。導出不能 (entry 候補ゼロ) も fixable reject。
    spread_max_pips : 許容 spread (pips)。quote spread がこれ超で fixable reject。
    pip_size : 旧 API 互換の単一 pip size。pip_size_for が無い pair の fallback。
    pip_size_for : pair → pip_size の解決関数。JPY クロスは 0.01、それ以外 0.0001 が
        既定 (Codex High#3: 固定 0.01 だと EURUSD の spread を過小評価する)。
    """

    def __init__(
        self,
        *,
        min_rr: float = 1.5,
        spread_max_pips: float = 2.0,
        pip_size: float = 0.01,
        pip_size_for=None,
    ) -> None:
        self._min_rr = min_rr
        self._spread_max_pips = spread_max_pips
        self._pip_size = pip_size
        self._pip_size_for = pip_size_for or _default_pip_size_for

    def _pip_size_of(self, pair: str | None) -> float:
        if pair is None:
            return self._pip_size
        return self._pip_size_for(pair)

    def pre_check(
        self,
        draft: ExecutionPlanDraft,
        context: dict[str, Any],
        *,
        include_executable_price: bool,
    ) -> RiskGateResult:
        """draft を context に対して検証する。

        include_executable_price (spec 2026-07-16 §2.A phase 分離):
          - False = planning phase (pipeline)。計画 RR (price 条件値) で判定。
          - True  = trigger phase (live final gate / shadow precheck)。trigger 時
            実行価格を候補に含め、breakout オーバーシュートを検出する。
        既定値は設けない — 呼び出し元に phase の選択を強制する (暗黙 default は
        planning/live の基準取り違えの再発経路)。
        structural を最優先で判定し、該当すれば即座に structural reject を返す
        (fixable と混在しても structural を返す = §5.3 の「構造的は再起案しない」)。
        """
        structural = self._structural_issues(context)
        if structural:
            return RiskGateResult(passed=False, reject_class=STRUCTURAL, issues=structural)

        # derive_rr は 1 回だけ計算し、fixable 判定と結果 (derived_rr) の両方で使う
        # (以前は _fixable_issues 内で二重に呼んでいた)。structural を通過した経路のみ
        # RR を導出する = structural reject の derived_rr は None のまま。
        derived = derive_rr(
            draft, context.get("quote"),
            include_executable_price=include_executable_price,
        )
        fixable = self._fixable_issues(draft, context, derived_rr=derived)
        if fixable:
            return RiskGateResult(
                passed=False, reject_class=FIXABLE, issues=fixable, derived_rr=derived,
            )

        return RiskGateResult(passed=True, reject_class=None, issues=[], derived_rr=derived)

    # ── structural (B): 再起案不可 ──────────────────────────────

    def _structural_issues(self, context: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        risk = context.get("risk_state", {})
        if risk.get("halt", "none") != "none":
            issues.append(f"halt active: {risk.get('halt')}")
        # DecisionContextBuilder._empty_risk_state() は bridge_health='ok' を返す。
        # 'ok'/'healthy' を健全とみなす (enum 不整合で実 context が常に reject されるのを防ぐ)。
        if risk.get("bridge_health", "ok") not in _HEALTHY_BRIDGE:
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

    def _fixable_issues(
        self, draft, context: dict[str, Any], *, derived_rr: float | None,
    ) -> list[str]:
        issues: list[str] = []
        action = draft.action
        # mid は untrusted quote 由来 — 非数値 (str 等) だと下の side 比較 (sl >= mid)
        # が TypeError で gate を殺す。mid が「存在するのに非数値/非有限」なら side 違反を
        # 黙って見逃さず fixable reject に倒す (R4 follow-up: skip すると sl>=entry 等の
        # 幾何違反が escape する)。mid 欠落 (None) は判定不能なので従来通り side チェック
        # skip (issue なし)。
        # NOTE: derive_rr の _executable_price も別途 quote を float 化するが、あちらは
        # direction で ask/bid を選ぶ「実行価格」、こちらは side 幾何の「entry proxy=mid」
        # と選ぶフィールドも用途も異なる。共有ヘルパにせず各所で独立に coerce する
        # (float() try/except の 3 行を共通化する DRY 益 < フィールド選択を絡める結合コスト)。
        mid = context.get("quote", {}).get("mid")
        if mid is not None:
            try:
                mid_val = float(mid)
            except (TypeError, ValueError):
                mid_val = None
            if mid_val is None or not math.isfinite(mid_val):
                issues.append("entry price invalid")
                mid = None  # side チェックを skip (下の mid is not None ガードで)
            else:
                mid = mid_val

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

        # RR: LLM 申告 (action["rr"]) は信用せず derive_rr の導出値で判定する
        # (spec 2026-07-16 §2.B — 申告過大の偽 pass / 申告過小の誤 reject を両方塞ぐ)。
        # 導出不能は楽観通過させず fixable reject (spread unknown と同じ思想)。
        # RR は pre_check で 1 回だけ導出済み — ここでは引数の値を使う (再計算しない)。
        derived = derived_rr
        if derived is None:
            if sl is not None and tp is not None:
                # sl/tp 欠落時は上の missing issue が既に立っている — entry 起因のみ追加。
                issues.append("rr underivable (no entry candidate)")
        elif derived < self._min_rr:
            claimed = action.get("rr")
            issues.append(
                f"derived rr {derived:.2f} below min {self._min_rr}"
                f" (claimed {claimed if claimed is not None else 'none'})"
            )

        # spread 上限。pip_size は pair 依存 (JPY=0.01, それ以外=0.0001)。
        # spread 不明 (None) は楽観通過させず fixable reject (Codex Low-Medium): 実 spread
        # が取れないまま発注判断品質を検証すると shadow 評価が楽観化する。
        # NaN/Inf/非数値も同様に楽観通過させない (R4 Medium#2): NaN>max が常に False で
        # ガードが黙って無効化される / 非数値は除算で TypeError → gate クラッシュ。
        spread = context.get("quote", {}).get("spread")
        pip_size = self._pip_size_of(context.get("pair"))
        if spread is None:
            issues.append("spread unknown")
        else:
            try:
                spread_val = float(spread)
            except (TypeError, ValueError):
                spread_val = None
            if spread_val is None or not math.isfinite(spread_val):
                issues.append("spread invalid")
            elif pip_size > 0:
                spread_pips = spread_val / pip_size
                if spread_pips > self._spread_max_pips:
                    issues.append(
                        f"spread {spread_pips:.1f}pips above max {self._spread_max_pips}"
                    )

        return issues
