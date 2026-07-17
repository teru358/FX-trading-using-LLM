"""RiskGateWorker.pre_check (Task 2.3) テスト。

design §5.2 Step5 / §5.3: deterministic な hard veto。reject を
A=修正可 (SL/TP side, RR, stops_level, comment) / B=構造的
(halt, bridge unhealthy, market closed, cooldown, stale required data) に分類。
advice_memo は不可侵 (risk gate は触らない)。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.orchestrator.risk_gate import RiskGateResult, RiskGateWorker, derive_rr
from src.orchestrator.schemas import EntryCondition, ExecutionPlanDraft, InvalidationCondition


def _draft(direction="long", sl=149.0, tp=152.0, rr=2.0) -> ExecutionPlanDraft:
    return ExecutionPlanDraft(
        direction=direction,
        entry_conditions=[EntryCondition(type="price_at_or_below", value=150.0)],
        action={"sl": sl, "tp": tp, "size_policy": "risk", "rr": rr, "comment": "x"},
        invalidation=[InvalidationCondition(type="price_below", value=148.0)],
        expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
        reasoning_summary="r",
    )


def _draft_entries(entries, *, direction="long", sl=149.0, tp=152.0) -> ExecutionPlanDraft:
    """entry_conditions を差し替えた draft を作る (derive_rr テスト用)。"""
    return ExecutionPlanDraft(
        direction=direction,
        entry_conditions=entries,
        action={"sl": sl, "tp": tp, "size_policy": "risk", "rr": 2.0, "comment": "x"},
        invalidation=[InvalidationCondition(type="price_below", value=148.0)],
        expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
        reasoning_summary="r",
    )


class TestDeriveRr:
    """derive_rr (spec 2026-07-16 §2.A): entry 候補 min の決定的 RR 導出 + phase 分離。"""

    QUOTE = {"bid": 149.99, "ask": 150.01, "mid": 150.0, "spread": 0.02}

    def test_planning_uses_condition_value_only(self) -> None:
        # planning phase: 条件値 150 のみ評価 → rr=(152-150)/(150-149)=2.0。
        # ask は含めない (押し目誤 reject 防止, R2 High#1)。
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=False)
        assert rr == pytest.approx(2.0)

    def test_live_includes_executable_price(self) -> None:
        # live phase: 候補 = 条件値 150 (rr 2.0) + ask 150.01 (rr≈1.97) → min≈1.97
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=True)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_pullback_plan_planning_pass_live_scenario(self) -> None:
        # R2 High#1 の例: long, 現在 ask=151, 押し目 entry=149.5, SL=148.5, TP=151.5。
        # planning (条件値のみ) → 計画 RR 2.0。実行価格を含めると 0.2 に落ちる —
        # planning で False を渡す限り誤 reject しない。
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=149.5)],
            sl=148.5, tp=151.5,
        )
        quote = {"bid": 150.99, "ask": 151.0, "mid": 150.995, "spread": 0.01}
        assert derive_rr(draft, quote, include_executable_price=False) == pytest.approx(2.0)
        assert derive_rr(draft, quote, include_executable_price=True) == pytest.approx(
            (151.5 - 151.0) / (151.0 - 148.5)
        )

    def test_short_uses_bid_as_executable(self) -> None:
        # short: reward=entry-tp, risk=sl-entry。実行価格は bid。
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_above", value=150.0)],
            direction="short", sl=151.0, tp=148.0,
        )
        rr = derive_rr(draft, self.QUOTE, include_executable_price=True)
        # 候補: 150 → (150-148)/(151-150)=2.0 / bid=149.99 → (1.99)/(1.01)≈1.970
        assert rr == pytest.approx((149.99 - 148.0) / (151.0 - 149.99))

    def test_multiple_price_conditions_takes_min(self) -> None:
        # R1 High#1 の例: long SL=149/TP=152、候補 150 (rr 2.0) と 151 (rr 0.5)
        # → 0.5 を採用 (SL に近い 150 は最良側)。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=151.0),
        ])
        rr = derive_rr(draft, None, include_executable_price=False)
        assert rr == pytest.approx(0.5)

    def test_live_overshoot_executable_price_dominates(self) -> None:
        # R1 High#2 の例: breakout=150, SL=149, TP=152, trigger 時 ask=151.8
        # → 実行 RR=(152-151.8)/(151.8-149)≈0.071 が min。
        draft = _draft_entries([EntryCondition(type="breakout_above", value=150.0)])
        quote = {"bid": 151.78, "ask": 151.8, "mid": 151.79, "spread": 0.02}
        rr = derive_rr(draft, quote, include_executable_price=True)
        assert rr == pytest.approx((152.0 - 151.8) / (151.8 - 149.0))

    def test_degenerate_candidate_counts_as_zero(self) -> None:
        # entry が TP を超えている候補 (reward<0) は rr=0.0 として min に参加。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=150.0),
            EntryCondition(type="breakout_above", value=152.5),  # > TP
        ])
        assert derive_rr(draft, None, include_executable_price=False) == 0.0

    def test_risk_nonpositive_candidate_counts_as_zero(self) -> None:
        # entry が SL の防御側に無い候補 (long で entry <= sl) は rr=0.0。
        draft = _draft_entries([
            EntryCondition(type="price_at_or_below", value=148.5),  # < SL
        ])
        assert derive_rr(draft, None, include_executable_price=False) == 0.0

    def test_nan_quote_counts_as_zero(self) -> None:
        # quote の ask が NaN → その候補は rr=0.0 (reject 方向)。NaN が min 比較を
        # すり抜けて偽 pass しない (R2 High#3)。
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        quote = {"bid": 149.99, "ask": float("nan"), "mid": 150.0, "spread": 0.02}
        assert derive_rr(draft, quote, include_executable_price=True) == 0.0

    def test_no_price_condition_falls_back_to_executable_even_in_planning(self) -> None:
        # price 条件ゼロの draft は planning でも実行価格 fallback (underivable 回避)。
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, self.QUOTE, include_executable_price=False)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_missing_bid_ask_falls_back_to_mid(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        rr = derive_rr(draft, {"mid": 150.0, "spread": None}, include_executable_price=True)
        assert rr == pytest.approx(2.0)

    def test_non_numeric_quote_degrades_to_none_not_crash(self) -> None:
        # quote は untrusted — str 値で TypeError にせず導出不能 (None) へ倒す。
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        quote = {"bid": "149.99", "ask": "not a number", "mid": None, "spread": 0.02}
        assert derive_rr(draft, quote, include_executable_price=True) is None

    def test_numeric_string_quote_coerced(self) -> None:
        # str 数値は float 化して使う (ローカル環境の JSON 復元経路対策)。
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        quote = {"ask": "150.01", "mid": 150.0}
        rr = derive_rr(draft, quote, include_executable_price=True)
        assert rr == pytest.approx((152.0 - 150.01) / (150.01 - 149.0))

    def test_no_candidates_returns_none(self) -> None:
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        assert derive_rr(draft, None, include_executable_price=True) is None
        assert derive_rr(draft, {}, include_executable_price=True) is None

    def test_missing_sl_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], sl=None,
        )
        assert derive_rr(draft, self.QUOTE, include_executable_price=False) is None

    def test_missing_tp_returns_none(self) -> None:
        draft = _draft_entries(
            [EntryCondition(type="price_at_or_below", value=150.0)], tp=None,
        )
        assert derive_rr(draft, self.QUOTE, include_executable_price=False) is None

    def test_live_phase_underivable_when_no_executable_price_at_all(self) -> None:
        # R4 High#1: include_executable_price=True で ask/bid/mid すべて取得不能 →
        # None (導出不能)。price 条件 (計画 RR 2.0) だけで pass させない。
        draft = _draft_entries([EntryCondition(type="breakout_above", value=150.0)])
        quote = {"ask": "invalid", "bid": "invalid", "mid": "invalid", "spread": 0.02}
        assert derive_rr(draft, quote, include_executable_price=True) is None

    def test_live_phase_underivable_when_quote_none(self) -> None:
        # include_executable_price=True で quote が None → 実行価格取れない → None。
        draft = _draft_entries([EntryCondition(type="breakout_above", value=150.0)])
        assert derive_rr(draft, None, include_executable_price=True) is None

    def test_planning_phase_unaffected_by_missing_executable(self) -> None:
        # planning phase (False) は実行価格を使わないので、quote が非数値でも
        # price 条件から計画 RR を導出する (回帰: 押し目 plan を壊さない)。
        draft = _draft_entries([EntryCondition(type="price_at_or_below", value=150.0)])
        quote = {"ask": "invalid", "bid": "invalid", "mid": "invalid"}
        assert derive_rr(draft, quote, include_executable_price=False) == pytest.approx(2.0)

    def test_planning_phase_no_price_condition_and_invalid_executable_is_none(self) -> None:
        # R4 review Low#2: planning phase で price 条件ゼロ + 実行価格非数値 →
        # elif fallback が None で潰れ candidates ゼロ → None (導出不能)。
        # (有効 quote なら fallback 成功する境界 = test_no_price_condition_..._planning)
        draft = _draft_entries([EntryCondition(type="spread_below", value_pips=2.0)])
        quote = {"ask": "invalid", "bid": "invalid", "mid": "invalid"}
        assert derive_rr(draft, quote, include_executable_price=False) is None


def _ctx(
    *,
    mid=150.0,
    halt="none",
    bridge_health="healthy",
    market_open=True,
    cooldown=False,
    technical_status="ok",
    spread=0.02,
) -> dict:
    return {
        "pair": "USDJPY=X",
        "quote": {"bid": mid - 0.01, "ask": mid + 0.01, "mid": mid, "spread": spread},
        "technical": {"status": technical_status},
        "risk_state": {
            "halt": halt,
            "bridge_health": bridge_health,
            "market_open": market_open,
            "cooldown": cooldown,
        },
    }


@pytest.fixture
def worker() -> RiskGateWorker:
    return RiskGateWorker(min_rr=1.5, spread_max_pips=2.0, pip_size=0.01)


class TestDerivedRrGate:
    """gate の RR 判定は申告値でなく derive_rr の導出値を使う (spec §2.B)。"""

    def test_overclaimed_rr_rejected_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=3.0 だが計画 RR は (150.5-150.0)/(150.0-149.0)=0.5 (tp=150.5)。
        # 旧実装 (申告比較) では pass していた偽 pass ケース。
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)

    def test_underclaimed_rr_passes_by_derived(self, worker: RiskGateWorker) -> None:
        # 申告 rr=0.5 だが計画 RR = 2.0 (>= 1.5)。旧実装では誤 reject していたケース。
        d = _draft(rr=0.5)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is True, res.issues

    def test_missing_rr_claim_passes_when_derivable(self, worker: RiskGateWorker) -> None:
        # 申告 rr 欠落は reject 理由にしない (導出できるため)。missing rr issue は廃止。
        d = _draft(rr=None)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        assert res.passed is True, res.issues

    def test_issue_message_includes_claimed(self, worker: RiskGateWorker) -> None:
        d = _draft(tp=150.5, rr=3.0)
        res = worker.pre_check(d, _ctx(), include_executable_price=False)
        msg = next(i for i in res.issues if "derived rr" in i)
        assert "claimed 3.0" in msg

    def test_rr_underivable_is_fixable(self, worker: RiskGateWorker) -> None:
        # price 条件なし + quote なし → 候補ゼロ → underivable reject。
        # (sl/tp はあるので missing sl/tp issue は立たない)
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="spread_below", value_pips=2.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        ctx = _ctx()
        ctx["quote"] = {}  # bid/ask/mid なし (spread も無いが underivable を先に確認)
        res = worker.pre_check(d, ctx, include_executable_price=True)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("underivable" in i for i in res.issues)

    def test_live_overshoot_rejected(self, worker: RiskGateWorker) -> None:
        # R1 High#2: breakout=150/SL=149/TP=152、trigger 時 ask≈151.8
        # → 実行 RR≈0.07 < 1.5 → live phase (include_executable_price=True) で reject。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="breakout_above", value=150.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        res = worker.pre_check(d, _ctx(mid=151.79), include_executable_price=True)
        assert res.passed is False
        assert any("derived rr" in i for i in res.issues)

    def test_live_executable_price_invalid_is_underivable_reject(self, worker: RiskGateWorker) -> None:
        # R4 High#1: live gate で実行価格が全て非数値 → underivable fixable reject。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="breakout_above", value=150.0)],
            action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        ctx = _ctx()
        ctx["quote"] = {"ask": "invalid", "bid": "invalid", "mid": "invalid", "spread": 0.02}
        res = worker.pre_check(d, ctx, include_executable_price=True)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("underivable" in i for i in res.issues)

    def test_pullback_plan_passes_planning_phase(self, worker: RiskGateWorker) -> None:
        # R2 High#1: 押し目 plan (entry=149.5 が現在 ask≈151 から遠い) は planning
        # phase で誤 reject しない (計画 RR = (151.5-149.5)/(149.5-148.5) = 2.0)。
        d = ExecutionPlanDraft(
            direction="long",
            entry_conditions=[EntryCondition(type="price_at_or_below", value=149.5)],
            action={"sl": 148.5, "tp": 151.5, "size_policy": "risk", "rr": 2.0, "comment": "x"},
            invalidation=[InvalidationCondition(type="price_below", value=148.0)],
            expires_at=datetime(2026, 6, 21, 18, 0, 0, tzinfo=timezone.utc),
            reasoning_summary="r",
        )
        res = worker.pre_check(d, _ctx(mid=151.0), include_executable_price=False)
        assert res.passed is True, res.issues


class TestRealContextIntegration:
    """DecisionContextBuilder 由来の risk_state enum と整合すること (Codex High#1)。

    context_builder._empty_risk_state() は bridge_health='ok' を返す。risk_gate が
    'healthy' しか通さないと実 context で常に structural reject になる。
    """

    def test_real_empty_risk_state_passes(self, worker: RiskGateWorker) -> None:
        from src.orchestrator.context_builder import DecisionContextBuilder

        ctx = _ctx()
        ctx["risk_state"] = DecisionContextBuilder._empty_risk_state()
        ctx["technical"]["status"] = "ok"
        res = worker.pre_check(_draft(), ctx, include_executable_price=False)
        assert res.passed is True, res.issues

    def test_bridge_ok_is_healthy(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(
            _draft(), _ctx(bridge_health="ok"), include_executable_price=False
        )
        assert res.passed is True


class TestPass:
    def test_clean_long_passes(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(_draft(), _ctx(), include_executable_price=False)
        assert res.passed is True
        assert res.reject_class is None
        assert res.issues == []

    def test_clean_short_passes(self, worker: RiskGateWorker) -> None:
        # 導出 RR = (150-148)/(151-150) = 2.0 (entry 候補 150.0、短方向)。
        draft = _draft(direction="short", sl=151.0, tp=148.0, rr=2.0)
        res = worker.pre_check(draft, _ctx(), include_executable_price=False)
        assert res.passed is True, res.issues

    def test_result_is_json_safe(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(_draft(), _ctx(), include_executable_price=False)
        d = res.to_dict()
        assert d["passed"] is True
        assert d["reject_class"] is None
        assert isinstance(d["issues"], list)


class TestStructuralReject:
    def test_halt_is_structural(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(_draft(), _ctx(halt="hard"), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "structural"
        assert any("halt" in i for i in res.issues)

    def test_bridge_unhealthy_is_structural(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(
            _draft(), _ctx(bridge_health="unhealthy"), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "structural"

    def test_market_closed_is_structural(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(
            _draft(), _ctx(market_open=False), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "structural"

    def test_cooldown_is_structural(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(
            _draft(), _ctx(cooldown=True), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "structural"

    def test_stale_technical_is_structural(self, worker: RiskGateWorker) -> None:
        res = worker.pre_check(
            _draft(), _ctx(technical_status="stale"), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "structural"


class TestFixableReject:
    def test_long_sl_above_entry_is_fixable(self, worker: RiskGateWorker) -> None:
        # long で SL が entry(mid) より上 = 不正な side
        draft = _draft(direction="long", sl=151.0, tp=153.0, rr=2.0)
        res = worker.pre_check(draft, _ctx(mid=150.0), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"

    def test_long_tp_below_entry_is_fixable(self, worker: RiskGateWorker) -> None:
        draft = _draft(direction="long", sl=149.0, tp=149.5, rr=2.0)
        res = worker.pre_check(draft, _ctx(mid=150.0), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"

    def test_rr_below_min_is_fixable(self, worker: RiskGateWorker) -> None:
        # 計画 RR = (150.5-150.0)/(150.0-149.0) = 0.5 < 1.5
        res = worker.pre_check(_draft(tp=150.5), _ctx(), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("derived rr" in i for i in res.issues)

    def test_spread_too_wide_is_fixable(self, worker: RiskGateWorker) -> None:
        # spread 0.05 = 5 pips > 2.0 max
        res = worker.pre_check(
            _draft(), _ctx(spread=0.05), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "fixable"

    def test_spread_unknown_is_fixable(self, worker: RiskGateWorker) -> None:
        """spread が None (不明) なら楽観通過させず fixable reject (Codex Low-Medium)。

        実 spread が取れない (bid/ask 無し) 場合に通過させると shadow 評価が楽観化する。
        """
        res = worker.pre_check(
            _draft(), _ctx(spread=None), include_executable_price=False
        )
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("spread" in i.lower() for i in res.issues)

    def test_spread_nan_is_invalid(self, worker: RiskGateWorker) -> None:
        # R4 Medium#2: spread=NaN は NaN>max が常に False で黙って通過する穴 → invalid reject。
        d = _draft()
        ctx = _ctx()
        ctx["quote"] = dict(ctx["quote"])
        ctx["quote"]["spread"] = float("nan")
        res = worker.pre_check(d, ctx, include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("spread invalid" in i for i in res.issues)

    def test_spread_non_numeric_is_invalid(self, worker: RiskGateWorker) -> None:
        # spread=非数値 str は除算で TypeError → gate クラッシュしていた → invalid reject。
        d = _draft()
        ctx = _ctx()
        ctx["quote"] = dict(ctx["quote"])
        ctx["quote"]["spread"] = "wide"
        res = worker.pre_check(d, ctx, include_executable_price=False)
        assert res.passed is False
        assert any("spread invalid" in i for i in res.issues)

    def test_spread_inf_is_invalid(self, worker: RiskGateWorker) -> None:
        d = _draft()
        ctx = _ctx()
        ctx["quote"] = dict(ctx["quote"])
        ctx["quote"]["spread"] = float("inf")
        res = worker.pre_check(d, ctx, include_executable_price=False)
        assert res.passed is False
        assert any("spread invalid" in i for i in res.issues)

    def test_eurusd_spread_uses_pip_0001(self, worker: RiskGateWorker) -> None:
        # Codex High#3: EURUSD の 3 pips = 0.0003。pip_size=0.01 固定だと
        # 0.0003/0.01=0.03 pips と過小評価され見逃す。pair 依存なら 3 pips で reject。
        ctx = _ctx(spread=0.0003)
        ctx["pair"] = "EURUSD=X"
        res = worker.pre_check(_draft(), ctx, include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"
        assert any("spread" in i.lower() for i in res.issues)

    def test_eurusd_tight_spread_passes(self, worker: RiskGateWorker) -> None:
        ctx = _ctx(spread=0.0001)  # 1 pip < 2.0
        ctx["pair"] = "EURUSD=X"
        res = worker.pre_check(_draft(), ctx, include_executable_price=False)
        assert res.passed is True, res.issues

    def test_missing_sl_is_fixable(self, worker: RiskGateWorker) -> None:
        draft = _draft()
        draft.action.pop("sl")
        res = worker.pre_check(draft, _ctx(), include_executable_price=False)
        assert res.passed is False
        assert res.reject_class == "fixable"


class TestPrecedence:
    def test_structural_beats_fixable(self, worker: RiskGateWorker) -> None:
        # halt(structural) と 導出 rr<min(fixable, tp=150.5 → 計画 RR 0.5) が同時
        # → structural を優先
        draft = _draft(tp=150.5)
        res = worker.pre_check(draft, _ctx(halt="hard"), include_executable_price=False)
        assert res.reject_class == "structural"

    def test_advice_memo_not_touched(self, worker: RiskGateWorker) -> None:
        # risk gate は advice_memo を読まない/変更しない (不可侵)。
        # ctx に advice_memo を入れても結果に影響しないことを確認。
        ctx = _ctx()
        ctx["policy"] = {"advice_memo": "ALWAYS GO LONG"}
        res = worker.pre_check(_draft(), ctx, include_executable_price=False)
        assert res.passed is True
