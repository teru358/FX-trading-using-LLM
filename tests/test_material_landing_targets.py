from datetime import datetime, timedelta

from src.orchestrator.material_landing import MaterialLandingDetector, PlanningTarget


def _now():
    return datetime(2026, 7, 17, 0, 0, 0)


def _detector(**kw):
    base = dict(
        get_latest_technical=lambda p: None,
        material_bias_delta_min=0.20,
        pairs=["EURUSD=X"],
        debounce_window_seconds=0,
        min_planning_interval_seconds=1800,
    )
    base.update(kw)
    return MaterialLandingDetector(**base)


def test_planning_target_cadence_when_no_material():
    """material 無し pair は periodic floor で triggers=() (cadence) として起動。"""
    det = _detector()
    targets = det.pairs_to_plan(_now())
    assert targets == [PlanningTarget(pair="EURUSD=X", triggers=())]


def test_planning_target_collects_multiple_triggers():
    """news + regime が同時に material なら triggers に両方入る (短絡しない)。

    debounce_window_seconds=0 でも初回呼び出しは窓を開始するだけで fire しない。
    一度呼んで窓を開き、次の評価 (>= 窓) で fire させる。
    """
    now = _now()
    det = _detector(
        get_news_impact=lambda p: 0.9, material_news_impact_min=0.5,
        get_news_key=lambda p: "k1",
    )
    det.mark_regime("EURUSD=X", "active")        # normal→active で rank 上昇 push
    det.pairs_to_plan(now)                        # 窓開始 (fire しない)
    targets = det.pairs_to_plan(now + timedelta(seconds=1))   # 窓を抜けて fire
    assert len(targets) == 1
    t = targets[0]
    assert t.pair == "EURUSD=X"
    assert "news" in t.triggers
    assert "regime" in t.triggers
