"""MaterialLandingDetector (spec §5.4)。

保存済み store を pull し「前回 planning 参照から material に変わった trade pair」を
検出する。trade instrument のみ対象 (§4.8/§5.3/§5.4)。状態は in-memory。

material の判定経路は 3 つ:
- technical: 実 _TechnicalSnapshot (analysis_store) の direction_bias / bias_score /
  collect_status を「前回 planning 参照」と比較し、反転・bias 変化・stale→ok 復帰を material とする。
- news: pair 関連の高 impact news。同じ news (同 key) を二度消費しないよう consumed-key を持つ。
- event: 高重要度 econ event window 内。同じ window を二度消費しないよう consumed-key を持つ。

planning を起こしたら状態を確定する:
- mark_committed(): snapshot 作成まで到達した (成功) ときに呼ぶ。technical baseline と
  news/event の consumed-key を更新し、last_planned を進める。
- mark_attempted(): snapshot 作成前に失敗したときに呼ぶ。debounce 窓だけ閉じ、baseline /
  consumed-key は更新しない (材料を読めていないので消費扱いにしない)。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


# market state の序列 (regime 上昇判定用)。detector は state 文字列の順位だけ知ればよい。
_REGIME_RANK = {"calm": 0, "normal": 1, "active": 2, "critical": 3}


@dataclass(frozen=True)
class PlanningTarget:
    pair: str
    triggers: tuple[str, ...]   # ("news", "regime", ...) 空なら cadence floor 起因


@dataclass
class _Seen:
    direction_bias: str | None
    bias_score: float | None
    collect_status: str | None
    news_key: str | None
    event_key: str | None
    # 最後に planning で消費した regime 上昇 sequence。current_seq > これ なら再 material
    # (= 前回 planning 以降に新たな rank 上昇イベントが起きた)。
    regime_seq: int = 0


class MaterialLandingDetector:
    def __init__(
        self,
        *,
        get_latest_technical: Callable[[str], Any],
        material_bias_delta_min: float,
        material_direction_flip_min: float = 0.10,
        get_news_impact: Callable[[str], float] | None = None,
        material_news_impact_min: float = 0.5,
        get_news_key: Callable[[str], str | None] | None = None,
        in_event_window: Callable[[str], bool] | None = None,
        get_event_key: Callable[[str], str | None] | None = None,
        debounce_window_seconds: int = 180,
        min_planning_interval_seconds: int = 1800,
        pairs: list[str] | None = None,
    ) -> None:
        self._get_tech = get_latest_technical
        self._bias_delta_min = material_bias_delta_min
        self._flip_min = material_direction_flip_min
        self._get_news_impact = get_news_impact
        self._news_impact_min = material_news_impact_min
        self._get_news_key = get_news_key
        self._in_event_window = in_event_window
        self._get_event_key = get_event_key
        self._seen: dict[str, _Seen] = {}
        self._debounce_window = debounce_window_seconds
        self._floor = min_planning_interval_seconds
        self._pairs = list(pairs or [])
        self._material_since: dict[str, datetime] = {}
        self._last_planned: dict[str, datetime] = {}
        # regime push (§5.4① / Task C-3): market_state_loop が別スレッドから mark_regime() で
        # market state を push する。planning_loop が pairs_to_plan() で読む。両スレッドが
        # 触れるため lock で保護する。
        #
        # 「rank が上昇した回数」を sequence でカウントする (消費は sequence 単位)。これにより
        # normal→active→critical の連続上昇も active→normal→active の出戻り上昇も各々 1 回の
        # イベントとして再計画でき、active 維持中の連発だけを抑える (consumed_seq で防ぐ)。
        self._regime_lock = threading.Lock()
        self._last_pushed_rank: dict[str, int] = {}  # 直近 push の rank (上昇検知用)
        self._regime_seq: dict[str, int] = {}        # pair ごとの累積上昇イベント数

    # ── 個別 material 判定 ──────────────────────────────────────────

    def technical_material(self, pair: str) -> bool:
        snap = self._get_tech(pair)
        if snap is None:
            return False
        prev = self._seen.get(pair)
        if prev is None:
            return True  # 初観測は material
        status = getattr(snap, "collect_status", None)
        if prev.collect_status != "ok" and status == "ok":
            return True  # stale/missing → ok 復帰
        direction = getattr(snap, "direction_bias", None)
        bias = getattr(snap, "bias_score", None)
        # direction 反転 かつ 前後いずれかの bias が閾値以上 (強い方向性の変化) → material。
        # 両側とも弱い (deadband 出入りの振動) 反転は抑制する (spec §2.C, codex Medium-2)。
        if prev.direction_bias != direction and bias is not None and prev.bias_score is not None:
            if max(abs(prev.bias_score), abs(bias)) >= self._flip_min:
                return True
        # bias 変化量 (方向不変でも大きく動けば material)
        if prev.bias_score is not None and bias is not None:
            if abs(bias - prev.bias_score) >= self._bias_delta_min:
                return True
        return False

    def news_material(self, pair: str) -> bool:
        if self._get_news_impact is None:
            return False
        if self._get_news_impact(pair) < self._news_impact_min:
            return False
        # 同じ news (同 key) を既に消費していれば material でない (再発火防止)。
        key = self._get_news_key(pair) if self._get_news_key is not None else None
        prev = self._seen.get(pair)
        if prev is not None and key is not None and prev.news_key == key:
            return False
        return True

    def event_window_material(self, pair: str) -> bool:
        if self._in_event_window is None:
            return False
        if not self._in_event_window(pair):
            return False
        # 同じ event window (同 key) を既に消費していれば material でない。
        key = self._get_event_key(pair) if self._get_event_key is not None else None
        prev = self._seen.get(pair)
        if prev is not None and key is not None and prev.event_key == key:
            return False
        return True

    def mark_regime(self, pair: str, state: str) -> None:
        """market_state_loop から現在の market state を push する (§5.4① / Task C-3)。

        別スレッド (market_state_loop) から呼ばれるため lock で保護する。直近 push の rank
        より **上昇** したとき (= →active / →critical の regime 変化) だけ上昇 sequence を
        進める。同等・下降では seq を据え置く (下降は再計画イベントにしない、§5.2)。
        active 維持中の連発は consumed_seq (commit_seen で消費) が抑える。
        """
        rank = _REGIME_RANK.get(state, 1)
        with self._regime_lock:
            prev_rank = self._last_pushed_rank.get(pair, _REGIME_RANK["normal"])
            if rank > prev_rank:
                self._regime_seq[pair] = self._regime_seq.get(pair, 0) + 1
            self._last_pushed_rank[pair] = rank

    def regime_material(self, pair: str) -> bool:
        """前回 planning 以降に新たな rank 上昇イベントがあれば True。

        current_seq (累積上昇数) > consumed_seq (前回 planning で消費した数) で判定する。
        normal→active / active→critical / active→normal→active のいずれも各 1 回の上昇
        イベントとして material になり、active 維持中 (上昇なし) は再発火しない。
        """
        with self._regime_lock:
            current_seq = self._regime_seq.get(pair, 0)
        if current_seq == 0:
            return False  # 一度も上昇していない (起動時 normal/calm)
        prev = self._seen.get(pair)
        consumed_seq = prev.regime_seq if prev is not None else 0
        return current_seq > consumed_seq

    def is_material(self, pair: str) -> bool:
        """いずれかの経路で material なら True。"""
        return (
            self.technical_material(pair)
            or self.news_material(pair)
            or self.event_window_material(pair)
            or self.regime_material(pair)
        )

    # ── 状態確定 ────────────────────────────────────────────────────

    def commit_seen(self, pair: str) -> None:
        """現在状態を「前回参照」として記録する (technical baseline + news/event key を消費)。

        snapshot 作成まで到達した planning の後に呼ぶ。technical snapshot が無くても
        news/event の consumed-key は更新する (高 impact news 単独でも再発火を防ぐため)。
        """
        snap = self._get_tech(pair)
        news_key = self._get_news_key(pair) if self._get_news_key is not None else None
        event_key = self._get_event_key(pair) if self._get_event_key is not None else None
        with self._regime_lock:
            regime_seq = self._regime_seq.get(pair, 0)
        self._seen[pair] = _Seen(
            direction_bias=getattr(snap, "direction_bias", None) if snap is not None else None,
            bias_score=getattr(snap, "bias_score", None) if snap is not None else None,
            collect_status=getattr(snap, "collect_status", None) if snap is not None else None,
            news_key=news_key,
            event_key=event_key,
            # 消費した regime 上昇 sequence を記録 (これより上昇するまで regime_material は False)。
            regime_seq=regime_seq,
        )

    def mark_committed(self, pair: str, now: datetime) -> None:
        """planning が snapshot 作成まで到達した (成功) ら呼ぶ。

        debounce 窓を閉じ、material baseline / consumed-key を消費し、floor 起点を進める。
        """
        self._last_planned[pair] = now
        self._material_since.pop(pair, None)
        self.commit_seen(pair)

    def mark_attempted(self, pair: str, now: datetime) -> None:
        """planning が snapshot 作成前に失敗したら呼ぶ。

        debounce 窓だけ閉じる (毎 tick リトライで詰まらせない)。material baseline /
        consumed-key は更新しない — 材料を読めていないので消費扱いにしない (次回再評価される)。
        floor 起点も進めない (失敗を「実施済み」と見なさない)。
        """
        self._material_since.pop(pair, None)

    # ── 起動 pair 決定 ──────────────────────────────────────────────

    def _material_triggers(self, pair: str) -> tuple[str, ...]:
        """該当する全 material 経路を集める (短絡しない)。空なら non-material。"""
        triggers: list[str] = []
        if self.technical_material(pair):
            triggers.append("technical")
        if self.news_material(pair):
            triggers.append("news")
        if self.event_window_material(pair):
            triggers.append("event")
        if self.regime_material(pair):
            triggers.append("regime")
        return tuple(triggers)

    def pairs_to_plan(self, now: datetime) -> list["PlanningTarget"]:
        out: list[PlanningTarget] = []
        for pair in self._pairs:
            triggers = self._material_triggers(pair)
            fire = False
            if triggers:
                # material 経路: debounce 窓を抜けたら起動。
                started = self._material_since.get(pair)
                if started is None:
                    self._material_since[pair] = now  # 窓開始
                elif (now - started).total_seconds() >= self._debounce_window:
                    fire = True
            else:
                self._material_since.pop(pair, None)  # material 解消で窓リセット
                # periodic floor: material が無い pair も最低頻度で起動する。
                # 初回 (last_planned 未設定) は bootstrap として due 扱いにする。
                last = self._last_planned.get(pair)
                if last is None or (now - last).total_seconds() >= self._floor:
                    fire = True
            if fire:
                out.append(PlanningTarget(pair=pair, triggers=triggers))
        return out
