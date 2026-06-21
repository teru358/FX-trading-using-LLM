"""HindsightEvaluator (plan Phase 4 Task 4.2) — trigger 後の判断品質を後追い計測する。

shadow_trigger の trigger_price を起点に、horizon_seconds の窓の OHLCV を引いて
MFE-R / MAE-R / PnL-R を算出する。1R = |trigger_price - sl| (価格距離) で R 正規化する。
発注はしない (shadow boundary) — 純粋に「もし執行していたら」を後追いするだけ。

audit_post_hoc.compute_mfe_mae の MFE/MAE 計算思想を踏襲しつつ、こちらは:
- pnl ではなく R 倍率で返す (通貨/ロット非依存)
- entry=trigger_price 起点 (決済価格起点でない)
- SL/TP 到達判定と path-aware な PnL-R を加える
点が異なる。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

# (symbol, start, end) -> OHLCV DataFrame (High/Low/Close, DatetimeIndex)
OhlcvProvider = Callable[[str, datetime, datetime], "object"]

_LONG = frozenset({"long", "buy", "bullish"})
_SHORT = frozenset({"short", "sell", "bearish"})


@dataclass
class HindsightResult:
    """1 trigger 分の後追い計測結果。has_data=False なら評価不能。"""
    has_data: bool
    mfe_r: float | None = None        # 最大順行幅 / 1R (>= 0)
    mae_r: float | None = None        # 最大逆行幅 / 1R (<= 0)
    pnl_r: float | None = None        # 確定 R (SL/TP 到達 or horizon 末 mark-to-market)
    would_hit_sl: bool | None = None
    would_hit_tp: bool | None = None
    reasoning_summary: str = ""


class HindsightEvaluator:
    """trigger_price 起点で OHLCV から R ベースの outcome を算出する。"""

    def __init__(self, *, ohlcv_provider: OhlcvProvider) -> None:
        self._provider = ohlcv_provider

    def evaluate(
        self,
        *,
        pair: str,
        direction: str,
        trigger_price: float,
        sl: float | None,
        tp: float | None,
        triggered_at: datetime,
        horizon_seconds: int,
    ) -> HindsightResult:
        """trigger 後 horizon 窓の OHLCV から MFE-R/MAE-R/PnL-R を算出する。"""
        sign = self._direction_sign(direction)
        if sign == 0:
            return HindsightResult(
                has_data=False, reasoning_summary=f"unknown direction {direction!r}"
            )

        # 1R = |trigger - sl| (価格距離)。SL 欠落 / R=0 は評価不能 (0 除算回避)。
        if sl is None:
            return HindsightResult(
                has_data=False, reasoning_summary="no sl, cannot compute R"
            )
        risk = abs(trigger_price - sl)
        if risk <= 0:
            return HindsightResult(
                has_data=False, reasoning_summary="zero risk distance (trigger==sl)"
            )

        end = triggered_at + timedelta(seconds=horizon_seconds)
        df = self._provider(pair, triggered_at, end)
        if df is None or len(df) == 0:
            return HindsightResult(
                has_data=False, reasoning_summary="no ohlcv in horizon"
            )

        # 重複 index を除去 (concat 由来)。
        df = df[~df.index.duplicated(keep="last")]
        best = float(df["High"].max())   # 期間中の最高値
        worst = float(df["Low"].min())   # 期間中の最安値
        last_close = float(df["Close"].iloc[-1])

        # 順行 (favorable) / 逆行 (adverse) を direction に応じて選ぶ。
        if sign > 0:  # long: 上が順行
            favorable = best - trigger_price
            adverse = worst - trigger_price
        else:         # short: 下が順行
            favorable = trigger_price - worst
            adverse = trigger_price - best

        mfe_r = max(favorable, 0.0) / risk
        mae_r = min(adverse, 0.0) / risk

        would_hit_sl = self._touched(df, sl, sign, is_sl=True)
        would_hit_tp = tp is not None and self._touched(df, tp, sign, is_sl=False)

        # path-aware PnL-R。バー内で SL/TP 両 touch は path 不明なので保守的に SL 優先。
        if would_hit_sl:
            pnl_r = -1.0
            reason = "hit SL within horizon"
        elif would_hit_tp:
            pnl_r = (tp - trigger_price) * sign / risk
            reason = "hit TP within horizon"
        else:
            pnl_r = (last_close - trigger_price) * sign / risk
            reason = "mark-to-market at horizon end (no SL/TP touch)"

        return HindsightResult(
            has_data=True,
            mfe_r=mfe_r,
            mae_r=mae_r,
            pnl_r=pnl_r,
            would_hit_sl=would_hit_sl,
            would_hit_tp=would_hit_tp,
            reasoning_summary=reason,
        )

    @staticmethod
    def _direction_sign(direction: str) -> int:
        d = (direction or "").lower().strip()
        if d in _LONG:
            return 1
        if d in _SHORT:
            return -1
        return 0

    @staticmethod
    def _touched(df, level: float, sign: int, *, is_sl: bool) -> bool:
        """price が level に到達したか。

        long の SL は下抜け (Low<=sl)、TP は上抜け (High>=tp)。short は逆。
        """
        if (sign > 0) == is_sl:
            # long+SL or short+TP → 下方向への到達 (Low <= level)
            return bool((df["Low"] <= level).any())
        # long+TP or short+SL → 上方向への到達 (High >= level)
        return bool((df["High"] >= level).any())
