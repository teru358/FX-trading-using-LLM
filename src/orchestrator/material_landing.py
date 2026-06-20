"""MaterialLandingDetector (spec §5.4)。

保存済み store を pull し「前回 planning 参照から material に変わった trade pair」を
検出する。trade instrument のみ対象 (§4.8/§5.3/§5.4)。状態は in-memory。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
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
        get_news_impact: Callable[[str], float] | None = None,
        material_news_impact_min: float = 0.5,
        in_event_window: Callable[[str], bool] | None = None,
        debounce_window_seconds: int = 180,
        min_planning_interval_seconds: int = 1800,
        pairs: list[str] | None = None,
    ) -> None:
        self._get_tech = get_latest_technical
        self._bias_delta_min = material_bias_delta_min
        self._get_news_impact = get_news_impact
        self._news_impact_min = material_news_impact_min
        self._in_event_window = in_event_window
        self._seen: dict[str, _Seen] = {}
        self._debounce_window = debounce_window_seconds
        self._floor = min_planning_interval_seconds
        self._pairs = list(pairs or [])
        self._material_since: dict[str, datetime] = {}
        self._last_planned: dict[str, datetime] = {}

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

    def news_material(self, pair: str) -> bool:
        if self._get_news_impact is None:
            return False
        return self._get_news_impact(pair) >= self._news_impact_min

    def event_window_material(self, pair: str) -> bool:
        if self._in_event_window is None:
            return False
        return bool(self._in_event_window(pair))

    def is_material(self, pair: str) -> bool:
        """いずれかの経路で material なら True。"""
        return (
            self.technical_material(pair)
            or self.news_material(pair)
            or self.event_window_material(pair)
        )

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

    def pairs_to_plan(self, now: datetime) -> list[str]:
        out: list[str] = []
        for pair in self._pairs:
            fire = False
            # material 経路（debounce 付き）
            if self.is_material(pair):
                started = self._material_since.get(pair)
                if started is None:
                    self._material_since[pair] = now  # 窓開始
                elif (now - started).total_seconds() >= self._debounce_window:
                    fire = True
            else:
                self._material_since.pop(pair, None)  # material 解消で窓リセット
            # periodic floor
            last = self._last_planned.get(pair)
            if last is not None and (now - last).total_seconds() >= self._floor:
                fire = True
            if fire:
                out.append(pair)
        return out

    def mark_planned(self, pair: str, now: datetime) -> None:
        """planning を起動したら呼ぶ。debounce 窓を閉じ floor 起点を更新。"""
        self._last_planned[pair] = now
        self._material_since.pop(pair, None)
        self.commit_seen(pair)
