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
