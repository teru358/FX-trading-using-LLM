"""MarketStateDetector — 市場 state 検知 (Phase1 Task C, §4.8 / §5.2 / §5.2.1)。

pair ごとの直近観測 (move-rate / spread / position 近接 / bridge) から state
(calm / normal / active / critical) を決める非LLM 状態機械。

2 つの出力にのみ効く (§5.2):
1. cadence resolver の「②市場 state 経路」boost (active/critical で頻度↑)。
2. regime 変化イベント (→active / →critical で planning 再計画トリガ)。

**market state は執行を制御しない (§5.2)。** 執行は watch loop の tick 評価が担い、急変
即応は deterministic ルール監視 (PriceMonitorWorker) が担う。

**ヒステリシス必須 (§5.2.1):** 上げ (calm→active) は即時、下げ (active→calm) は安定継続を
要求する。閾値付近のバタつきを防ぐ。`now` は呼び出し側が注入する (clock 非依存)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.schema import OrchestratorMarketStateConfig

# state の序列 (上げ/下げ判定に使う)。
CALM = "calm"
NORMAL = "normal"
ACTIVE = "active"
CRITICAL = "critical"
_ORDER = {CALM: 0, NORMAL: 1, ACTIVE: 2, CRITICAL: 3}


@dataclass
class MarketObservation:
    """1 回分の市場観測。検知に必要な最小入力。"""
    move_pct: float = 0.0              # price_move_window 内の変動率 (絶対値)
    spread_pips: float | None = None   # 現在 spread (pips)。None = 不明
    position_near_sl_tp: bool = False  # 保有ポジションが SL/TP/breakeven 近傍
    has_position: bool = False         # ポジション保有中か
    bridge_degraded: bool = False      # bridge health 劣化
    important_news: bool = False       # 重要 news impact 着弾 (→active)


@dataclass
class _PairState:
    state: str = NORMAL
    # 下げ猶予タイマ起点 (この時刻から安定継続を測る)。None = 計測中でない。
    downgrade_since: datetime | None = None


class MarketStateDetector:
    """pair ごとに state を保持し、観測から遷移させる (ヒステリシス付)。"""

    def __init__(self, config: "OrchestratorMarketStateConfig") -> None:
        self._cfg = config
        self._pairs: dict[str, _PairState] = {}

    def state_of(self, pair: str) -> str:
        return self._pairs.get(pair, _PairState()).state

    def observe(self, pair: str, obs: MarketObservation, now: datetime) -> str:
        """観測から新 state を決めて返す。ヒステリシスを適用する。"""
        ps = self._pairs.setdefault(pair, _PairState())
        target = self._raw_target(obs)
        cur = ps.state

        if _ORDER[target] > _ORDER[cur]:
            # 上げは即時 (§5.2.1)。猶予タイマをリセット。
            ps.state = target
            ps.downgrade_since = None
        elif _ORDER[target] < _ORDER[cur]:
            # 下げは安定継続を要求。猶予を満たしたら 1 段ずつ下げる。
            if ps.downgrade_since is None:
                ps.downgrade_since = now
            elif (now - ps.downgrade_since).total_seconds() >= self._downgrade_grace(cur):
                ps.state = self._step_down(cur)
                # 1 段下げたら、まだ target に届かなければ次段の猶予を測り直す。
                ps.downgrade_since = now if _ORDER[ps.state] > _ORDER[target] else None
        else:
            # target == cur: 安定。猶予計測をクリア。
            ps.downgrade_since = None
        return ps.state

    def _raw_target(self, obs: MarketObservation) -> str:
        """ヒステリシス前の「観測が示す素の state」。"""
        cfg = self._cfg
        # critical: ポジション保護局面・spread 異常・bridge 劣化。
        if obs.bridge_degraded:
            return CRITICAL
        if obs.has_position and obs.position_near_sl_tp:
            return CRITICAL
        if obs.spread_pips is not None and obs.spread_pips >= cfg.spread_spike_pips:
            return CRITICAL
        # active: 変動率超 or 重要 news。
        if obs.move_pct >= cfg.active_move_pct or obs.important_news:
            return ACTIVE
        # 完全静穏 (変動率が active 閾値の半分未満 + ポジション無し) は calm を志向する。
        # calm への遷移は下げ猶予 (calm_after_seconds) を経るため即時には落ちない。
        if obs.move_pct < cfg.active_move_pct * 0.5 and not obs.has_position:
            return CALM
        # それ以外は normal。
        return NORMAL

    def _step_down(self, cur: str) -> str:
        if cur == CRITICAL:
            return NORMAL   # critical 解消は normal まで (active を経由しない)
        if cur == ACTIVE:
            return NORMAL
        if cur == NORMAL:
            return CALM
        return cur

    def _downgrade_grace(self, cur: str) -> int:
        """現 state から下げるのに要する安定継続秒。"""
        if cur == CALM:
            return 0
        if cur == NORMAL:
            return self._cfg.calm_after_seconds   # normal→calm は長め
        return self._cfg.normal_after_seconds     # active/critical→normal


def detector_with_horizon(
    config: "OrchestratorMarketStateConfig", horizon: str
) -> MarketStateDetector:
    """horizon (day/swing) で閾値を overlay した detector を作る (§5.2.1)。

    swing は active/critical に入りにくく (閾値を緩め)、day は入りやすくする。
    config を破壊しないよう複製してから閾値を調整する。
    """
    import copy

    cfg = copy.copy(config)
    if horizon == "swing":
        # 短期変動では active に上がりにくく (閾値を上げる)。
        cfg.active_move_pct = config.active_move_pct * 2.0
    elif horizon == "day":
        # 敏感に (閾値を下げる)。
        cfg.active_move_pct = config.active_move_pct * 0.5
    return MarketStateDetector(cfg)
