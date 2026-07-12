# tests/test_material_flip_min.py
import pytest

from src.orchestrator.material_landing import MaterialLandingDetector, _Seen


class _Snap:
    def __init__(self, direction_bias, bias_score, collect_status="ok"):
        self.direction_bias = direction_bias
        self.bias_score = bias_score
        self.collect_status = collect_status


def _seen_from(snap):
    return _Seen(
        direction_bias=snap.direction_bias, bias_score=snap.bias_score,
        collect_status=snap.collect_status, news_key=None, event_key=None,
    )


def _detector():
    holder = {"cur": None}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: holder["cur"],
        material_bias_delta_min=0.20,
        material_direction_flip_min=0.10,
    )
    return det, holder


def test_weak_flip_both_sides_not_material():
    # long +0.06 → short -0.06: 両側とも |bias| < 0.10、delta 0.12 < 0.20 → 非 material
    det, h = _detector()
    det._seen["USDJPY=X"] = _seen_from(_Snap("long", 0.06))
    h["cur"] = _Snap("short", -0.06)
    assert det.technical_material("USDJPY=X") is False


def test_strong_to_neutral_is_material():
    # long +0.15 → neutral -0.04: delta 0.19 < 0.20 だが max(0.15,0.04)=0.15 ≥ 0.10 → material
    det, h = _detector()
    det._seen["USDJPY=X"] = _seen_from(_Snap("long", 0.15))
    h["cur"] = _Snap("neutral", -0.04)
    assert det.technical_material("USDJPY=X") is True


def test_bias_delta_path_still_fires_without_flip():
    # 同方向 long、bias 0.1 → 0.4: direction 不変だが delta 0.3 ≥ 0.20 → material
    det, h = _detector()
    det._seen["USDJPY=X"] = _seen_from(_Snap("long", 0.1))
    h["cur"] = _Snap("long", 0.4)
    assert det.technical_material("USDJPY=X") is True


def test_strong_flip_material_when_both_conditions_met():
    # flip と delta の両方が成立する強い反転 (どちらでも material)。
    # prev long +0.09 → short -0.30: max(0.09,0.30)=0.30 ≥ flip_min(0.10) で flip 成立、
    # かつ |delta|=0.39 ≥ bias_delta_min(0.20) で delta も成立する。
    # なお「両側 |bias| < flip_min かつ |delta| ≥ bias_delta_min」は flip_min(0.10) <
    # bias_delta_min(0.20) より数学的に到達不能なので、そのケースはテストできない。
    det, h = _detector()
    det._seen["USDJPY=X"] = _seen_from(_Snap("long", 0.09))
    h["cur"] = _Snap("short", -0.30)
    assert det.technical_material("USDJPY=X") is True


def test_first_observation_material():
    det, h = _detector()
    h["cur"] = _Snap("long", 0.02)
    assert det.technical_material("USDJPY=X") is True  # prev なし


def test_status_recovery_material():
    det, h = _detector()
    prev = _seen_from(_Snap("long", 0.3, collect_status="failed"))
    det._seen["USDJPY=X"] = prev
    h["cur"] = _Snap("long", 0.3, collect_status="ok")
    assert det.technical_material("USDJPY=X") is True  # failed → ok 復帰


def test_flip_min_range_validation():
    from src.config.schema import OrchestratorFiringConfig
    with pytest.raises(ValueError):
        OrchestratorFiringConfig(material_direction_flip_min=1.5)
    with pytest.raises(ValueError):
        OrchestratorFiringConfig(material_direction_flip_min=-0.1)
    OrchestratorFiringConfig(material_direction_flip_min=0.1)  # OK


def test_config_default_flip_min():
    from src.config.schema import OrchestratorFiringConfig
    assert OrchestratorFiringConfig().material_direction_flip_min == 0.10
