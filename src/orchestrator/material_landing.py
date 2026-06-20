"""MaterialLandingDetector (spec §5.4)。

保存済み store を pull し「前回 planning 参照から material に変わった trade pair」を
検出する。trade instrument のみ対象 (§4.8/§5.3/§5.4)。状態は in-memory。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class _Seen:
    direction: str | None
    bias_score: float | None
    status: str | None


class MaterialLandingDetector:
    def __init__(
        self,
        *,
        get_latest_technical: Callable[[str], Any],
        material_bias_delta_min: float,
    ) -> None:
        self._get_tech = get_latest_technical
        self._bias_delta_min = material_bias_delta_min
        self._seen: dict[str, _Seen] = {}

    def technical_material(self, pair: str) -> bool:
        snap = self._get_tech(pair)
        if snap is None:
            return False
        prev = self._seen.get(pair)
        if prev is None:
            return True  # 初観測は material
        if prev.status != "ok" and getattr(snap, "status", None) == "ok":
            return True  # stale/missing → ok 復帰
        if prev.direction != snap.direction:
            return True  # direction 反転
        if prev.bias_score is not None and snap.bias_score is not None:
            if abs(snap.bias_score - prev.bias_score) >= self._bias_delta_min:
                return True
        return False

    def commit_seen(self, pair: str) -> None:
        """planning を起こした後、現在状態を「前回参照」として記録する。"""
        snap = self._get_tech(pair)
        if snap is None:
            return
        self._seen[pair] = _Seen(
            direction=getattr(snap, "direction", None),
            bias_score=getattr(snap, "bias_score", None),
            status=getattr(snap, "status", None),
        )
