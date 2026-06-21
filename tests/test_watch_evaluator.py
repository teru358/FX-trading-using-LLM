"""WatchEvaluator の純粋関数テスト (design §6.2 / §6.3 / §7.2)。

entry_conditions (AND) / invalidation / expiry / freshness final wall を
DB なしで決定的に評価する層。runtime はこれを合成して shadow trigger を組む。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.config.schema import OrchestratorEntryConfig
from src.orchestrator.schemas import EntryCondition, InvalidationCondition
from src.orchestrator.watch_evaluator import WatchEvaluator


def _ctx(
    *,
    mid: float = 150.10,
    spread: float = 0.01,
    tech_status: str = "ok",
    halt: str = "none",
    market_open: bool = True,
    news_conflict: bool = False,
    quote_age: float = 1.0,
    tech_age: float = 60.0,
    pair: str = "USDJPY=X",
) -> dict:
    return {
        "pair": pair,
        "quote": {"mid": mid, "spread": spread},
        "technical": {"status": tech_status, "age_sec": tech_age},
        "risk_state": {"halt": halt, "market_open": market_open},
        "news_conflict": news_conflict,
        "quote_age_sec": quote_age,
    }


# ── entry_conditions (§6.2, AND only) ──────────────────────────


def test_price_at_or_below_holds_when_mid_at_or_below_value() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    conds = [EntryCondition(type="price_at_or_below", value=150.30)]
    assert ev.entry_conditions_hold(conds, _ctx(mid=150.10)) is True
    assert ev.entry_conditions_hold(conds, _ctx(mid=150.30)) is True
    assert ev.entry_conditions_hold(conds, _ctx(mid=150.31)) is False


def test_price_at_or_above_and_breakout() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    assert ev.entry_conditions_hold(
        [EntryCondition(type="price_at_or_above", value=150.0)], _ctx(mid=150.0)
    ) is True
    # breakout_above is strict >
    assert ev.entry_conditions_hold(
        [EntryCondition(type="breakout_above", value=150.0)], _ctx(mid=150.0)
    ) is False
    assert ev.entry_conditions_hold(
        [EntryCondition(type="breakout_above", value=150.0)], _ctx(mid=150.01)
    ) is True
    assert ev.entry_conditions_hold(
        [EntryCondition(type="breakout_below", value=150.0)], _ctx(mid=149.99)
    ) is True


def test_spread_below_uses_pip_size() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    # USDJPY pip=0.01: spread 0.01 = 1.0 pips < 2.0 -> holds
    assert ev.entry_conditions_hold(
        [EntryCondition(type="spread_below", value_pips=2.0)], _ctx(spread=0.01)
    ) is True
    # spread 0.03 = 3.0 pips, not < 2.0 -> fails
    assert ev.entry_conditions_hold(
        [EntryCondition(type="spread_below", value_pips=2.0)], _ctx(spread=0.03)
    ) is False


def test_technical_status_is_ok() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    cond = [EntryCondition(type="technical_status_is", status="ok")]
    assert ev.entry_conditions_hold(cond, _ctx(tech_status="ok")) is True
    assert ev.entry_conditions_hold(cond, _ctx(tech_status="stale")) is False


def test_entry_conditions_are_anded() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    conds = [
        EntryCondition(type="price_at_or_below", value=150.30),
        EntryCondition(type="technical_status_is", status="ok"),
    ]
    # both hold
    assert ev.entry_conditions_hold(conds, _ctx(mid=150.10, tech_status="ok")) is True
    # one fails -> AND fails
    assert ev.entry_conditions_hold(conds, _ctx(mid=150.10, tech_status="stale")) is False


def test_empty_entry_conditions_do_not_hold() -> None:
    """空 entry_conditions は trigger 暴発を防ぐため False (vacuous truth にしない)。"""
    ev = WatchEvaluator(OrchestratorEntryConfig())
    assert ev.entry_conditions_hold([], _ctx()) is False


# ── invalidation (§6.3) ────────────────────────────────────────


def test_invalidation_price_below_above() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    now = datetime(2026, 6, 21, 9, 0, 0)
    expires = now + timedelta(days=1)
    assert ev.invalidation_reason(
        [InvalidationCondition(type="price_below", value=149.0)],
        _ctx(mid=148.9), now=now, expires_at=expires,
    ) == "price_below"
    assert ev.invalidation_reason(
        [InvalidationCondition(type="price_above", value=151.0)],
        _ctx(mid=151.5), now=now, expires_at=expires,
    ) == "price_above"
    # not breached
    assert ev.invalidation_reason(
        [InvalidationCondition(type="price_below", value=149.0)],
        _ctx(mid=150.0), now=now, expires_at=expires,
    ) is None


def test_invalidation_technical_stale_and_news_conflict() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    now = datetime(2026, 6, 21, 9, 0, 0)
    expires = now + timedelta(days=1)
    assert ev.invalidation_reason(
        [InvalidationCondition(type="technical_stale")],
        _ctx(tech_status="stale"), now=now, expires_at=expires,
    ) == "technical_stale"
    assert ev.invalidation_reason(
        [InvalidationCondition(type="news_conflict")],
        _ctx(news_conflict=True), now=now, expires_at=expires,
    ) == "news_conflict"


def test_expiry_detected_even_without_explicit_expired_marker() -> None:
    """expires_at を過ぎたら invalidation list に expired marker が無くても expired。"""
    ev = WatchEvaluator(OrchestratorEntryConfig())
    now = datetime(2026, 6, 21, 9, 0, 0)
    expires = datetime(2026, 6, 21, 8, 0, 0)  # already past
    assert ev.invalidation_reason([], _ctx(), now=now, expires_at=expires) == "expired"


def test_not_expired_before_expires_at() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    now = datetime(2026, 6, 21, 9, 0, 0)
    expires = datetime(2026, 6, 21, 10, 0, 0)
    assert ev.invalidation_reason([], _ctx(), now=now, expires_at=expires) is None


# ── freshness final wall (§7.2) ────────────────────────────────


def test_freshness_passes_clean_context() -> None:
    ev = WatchEvaluator(OrchestratorEntryConfig())
    assert ev.freshness_issues(_ctx()) == []


def test_freshness_blocks_stale_quote() -> None:
    cfg = OrchestratorEntryConfig(max_quote_age_seconds=10)
    ev = WatchEvaluator(cfg)
    issues = ev.freshness_issues(_ctx(quote_age=30.0))
    assert any("quote" in i for i in issues)


def test_freshness_blocks_stale_technical_when_required() -> None:
    cfg = OrchestratorEntryConfig(require_fresh_technical=True, max_technical_age_seconds=1800)
    ev = WatchEvaluator(cfg)
    # status not ok
    assert any("technical" in i for i in ev.freshness_issues(_ctx(tech_status="stale")))
    # age too old
    assert any("technical" in i for i in ev.freshness_issues(_ctx(tech_age=3600)))


def test_freshness_ignores_technical_when_not_required() -> None:
    cfg = OrchestratorEntryConfig(require_fresh_technical=False)
    ev = WatchEvaluator(cfg)
    assert ev.freshness_issues(_ctx(tech_status="stale", tech_age=99999)) == []


def test_freshness_blocks_when_quote_age_uncomputable() -> None:
    """quote_age_sec が None (時刻 parse 不能 / tz 混在) なら freshness を block する。

    age 不明を「鮮度 OK」と誤認すると tz 不整合時に古い quote で trigger しうる
    (過去に timezone 実害あり, Codex Medium)。安全側 = block。
    """
    ev = WatchEvaluator(OrchestratorEntryConfig())
    ctx = _ctx()
    ctx["quote_age_sec"] = None
    assert any("quote" in i for i in ev.freshness_issues(ctx))


def test_freshness_blocks_when_quote_age_missing() -> None:
    """quote_age_sec キーが欠落していても block する (None と同等扱い)。"""
    ev = WatchEvaluator(OrchestratorEntryConfig())
    ctx = _ctx()
    del ctx["quote_age_sec"]
    assert any("quote" in i for i in ev.freshness_issues(ctx))


def test_freshness_blocks_when_technical_age_uncomputable() -> None:
    """require_fresh_technical 時、technical.age_sec が None なら block する。"""
    cfg = OrchestratorEntryConfig(require_fresh_technical=True)
    ev = WatchEvaluator(cfg)
    ctx = _ctx()
    ctx["technical"]["age_sec"] = None
    assert any("technical" in i for i in ev.freshness_issues(ctx))


def test_freshness_blocks_wide_spread_halt_and_closed_market() -> None:
    cfg = OrchestratorEntryConfig(spread_max_pips=2.0)
    ev = WatchEvaluator(cfg)
    assert any("spread" in i for i in ev.freshness_issues(_ctx(spread=0.05)))
    assert any("halt" in i for i in ev.freshness_issues(_ctx(halt="risk_event")))
    assert any("market" in i for i in ev.freshness_issues(_ctx(market_open=False)))
