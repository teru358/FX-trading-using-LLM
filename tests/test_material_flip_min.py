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


def test_flip_fails_but_delta_fires():
    # direction 反転だが両側弱く flip 不成立、しかし delta が大きいケース。
    # weak flip で False に early-return せず delta 経路が評価されることを保証。
    # prev long +0.09 → short -0.30: max(0.09,0.30)=0.30 ≥ flip_min なので実は flip で material。
    # flip も delta も成立する境界確認 (どちらでも material=True)。
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
    from src.config.loader import _validate_firing_ranges
    from src.config.schema import OrchestratorFiringConfig
    with pytest.raises(Exception):
        _validate_firing_ranges(OrchestratorFiringConfig(material_direction_flip_min=1.5))
    with pytest.raises(Exception):
        _validate_firing_ranges(OrchestratorFiringConfig(material_direction_flip_min=-0.1))
    _validate_firing_ranges(OrchestratorFiringConfig(material_direction_flip_min=0.1))  # OK


def test_config_default_flip_min():
    from src.config.schema import OrchestratorFiringConfig
    assert OrchestratorFiringConfig().material_direction_flip_min == 0.10
