from src.orchestrator.material_landing import MaterialLandingDetector

class _FakeTechSnap:
    def __init__(self, direction, bias_score, status="ok"):
        self.direction = direction
        self.bias_score = bias_score
        self.status = status

def test_direction_change_is_material():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    # 初回は基準が無いので material（初観測）
    assert det.technical_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")
    # 同じ → not material
    assert det.technical_material("USDJPY=X") is False
    # direction 反転 → material
    snaps["USDJPY=X"] = _FakeTechSnap("short", 0.3)
    assert det.technical_material("USDJPY=X") is True

def test_bias_delta_below_threshold_not_material():
    snaps = {"EURUSD=X": _FakeTechSnap("long", 0.30)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    det.technical_material("EURUSD=X"); det.commit_seen("EURUSD=X")
    snaps["EURUSD=X"] = _FakeTechSnap("long", 0.40)  # delta 0.10 < 0.20
    assert det.technical_material("EURUSD=X") is False

def test_stale_to_ok_recovery_is_material():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3, status="stale")}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    det.technical_material("USDJPY=X"); det.commit_seen("USDJPY=X")
    snaps["USDJPY=X"] = _FakeTechSnap("long", 0.3, status="ok")  # stale→ok
    assert det.technical_material("USDJPY=X") is True

def test_news_impact_above_min_is_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.7,        # pair 関連通貨の impact
        material_news_impact_min=0.5,
    )
    assert det.news_material("USDJPY=X") is True

def test_news_impact_below_min_not_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.3,
        material_news_impact_min=0.5,
    )
    assert det.news_material("USDJPY=X") is False

def test_in_high_importance_event_window_is_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        in_event_window=lambda pair: True,
    )
    assert det.event_window_material("USDJPY=X") is True

from datetime import datetime, timezone, timedelta

def _t(s):  # 秒オフセットのヘルパ
    return datetime(2026, 6, 20, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=s)

def test_debounce_coalesces_rapid_landings():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["USDJPY=X"],
    )
    # t=0: material（初観測）→ debounce 窓開始、まだ起動しない
    assert det.pairs_to_plan(_t(0)) == []
    # t=60: 窓内 → まだ
    assert det.pairs_to_plan(_t(60)) == []
    # t=200: 窓（180s）を抜けた → 起動
    assert det.pairs_to_plan(_t(200)) == ["USDJPY=X"]

def test_periodic_floor_fires_without_material():
    snaps = {"EURUSD=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["EURUSD=X"],
    )
    det.pairs_to_plan(_t(0)); det.mark_planned("EURUSD=X", _t(0))
    # material 無し・floor 未超過 → 起動しない
    snaps["EURUSD=X"] = _FakeTechSnap("long", 0.3)  # 変化なし
    assert det.pairs_to_plan(_t(900)) == []
    # floor（1800s）超過 → 起動
    assert det.pairs_to_plan(_t(1801)) == ["EURUSD=X"]
