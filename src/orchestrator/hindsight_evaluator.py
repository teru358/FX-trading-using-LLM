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
from typing import TYPE_CHECKING, Callable

from src.orchestrator.watch_evaluator import _pip_size_for

if TYPE_CHECKING:
    from src.data.price_store import PriceStore

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
    pnl_r: float | None = None        # 確定 R (SL/TP 到達 or horizon 末 mark-to-market、spread 控除後)
    would_hit_sl: bool | None = None
    would_hit_tp: bool | None = None
    reasoning_summary: str = ""
    spread_cost_r: float | None = None  # pnl_r から控除した spread コスト内訳 (spec D-8)


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
        spread_pips: float | None = None,
    ) -> HindsightResult:
        """trigger 後 horizon 窓の OHLCV から MFE-R/MAE-R/PnL-R を算出する。

        spread_pips (trigger 時点の spread) を渡すと、day trading の spread/TP 比が
        swing より高いことによる過大評価を補正するため、spread コストを R 換算して
        pnl_r から控除する (spec D-8)。控除額は spread_cost_r に内訳として残す
        (pnl_r は NET、spread_cost_r が分かれば gross = pnl_r + spread_cost_r で復元可能)。
        None または 0 以下なら控除しない (spread_cost_r=None のまま)。
        """
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

        # spread コストを R 換算 (spec D-8)。未提供 / 0 以下は控除なし (swing 由来の
        # 既存行や spread 不明ケースは gross のまま = 挙動不変)。
        spread_cost_r: float | None = None
        if spread_pips is not None and spread_pips > 0:
            spread_cost_r = (spread_pips * _pip_size_for(pair)) / risk

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

        # path-aware PnL-R。バーを時系列走査し最初に到達した SL/TP を採用する。
        # 同一バー内で両方タッチした場合のみ path 不明として保守的に SL 優先。
        first_hit = self._first_hit(df, sl, tp, sign)
        if first_hit == "sl":
            pnl_r = -1.0
            would_hit_sl, would_hit_tp = True, False
            reason = "hit SL first within horizon"
        elif first_hit == "tp":
            pnl_r = (tp - trigger_price) * sign / risk
            would_hit_sl, would_hit_tp = False, True
            reason = "hit TP first within horizon"
        else:
            pnl_r = (last_close - trigger_price) * sign / risk
            would_hit_sl, would_hit_tp = False, False
            reason = "mark-to-market at horizon end (no SL/TP touch)"

        # spread コストを控除して NET 化 (SL/TP/mark-to-market いずれの分岐も対象)。
        if spread_cost_r is not None:
            pnl_r -= spread_cost_r

        return HindsightResult(
            has_data=True,
            mfe_r=mfe_r,
            mae_r=mae_r,
            pnl_r=pnl_r,
            would_hit_sl=would_hit_sl,
            would_hit_tp=would_hit_tp,
            reasoning_summary=reason,
            spread_cost_r=spread_cost_r,
        )

    @staticmethod
    def _direction_sign(direction: str) -> int:
        d = (direction or "").lower().strip()
        if d in _LONG:
            return 1
        if d in _SHORT:
            return -1
        return 0

    @classmethod
    def _first_hit(cls, df, sl: float, tp: float | None, sign: int) -> str | None:
        """バーを時系列走査し、最初に到達した側を返す ('sl' | 'tp' | None)。

        各バーで SL / TP のタッチを判定し、先に到達したバーの側を確定する。
        同一バー内で両方タッチした場合は path 不明なので保守的に SL を優先する。
        OHLCV は始値→終値の bar 内 path が不明なため、bar 単位 (= 利用可能な最小粒度)
        での順序判定が現実的な近似。
        """
        for ts in df.index:
            high = float(df.at[ts, "High"])
            low = float(df.at[ts, "Low"])
            sl_touch = cls._bar_touches(high, low, sl, sign, is_sl=True)
            tp_touch = (
                tp is not None and cls._bar_touches(high, low, tp, sign, is_sl=False)
            )
            if sl_touch and tp_touch:
                return "sl"   # 同一バー両タッチ → 保守的に SL 優先
            if sl_touch:
                return "sl"
            if tp_touch:
                return "tp"
        return None

    @staticmethod
    def _bar_touches(high: float, low: float, level: float, sign: int, *, is_sl: bool) -> bool:
        """1 バー (high/low) が level に到達したか。

        long の SL は下抜け (low<=sl)、TP は上抜け (high>=tp)。short は逆。
        """
        if (sign > 0) == is_sl:
            # long+SL or short+TP → 下方向への到達 (low <= level)
            return low <= level
        # long+TP or short+SL → 上方向への到達 (high >= level)
        return high >= level


def make_hindsight_evaluator(price_store: "PriceStore") -> HindsightEvaluator:
    """PriceStore.load_ohlcv をラップして HindsightEvaluator を組み立てる。

    Phase 6 (main.py 配線) の injection point。OrchestratorRuntime に
    `hindsight_evaluator=make_hindsight_evaluator(price_store)` を渡すと、trigger 後の
    OHLCV を既存 OHLCV キャッシュから引いて R を後追い計測する。
    make_news_provider と同じアダプタ方針 (runtime は PriceStore に直接依存しない)。
    """
    def provider(symbol: str, start: datetime, end: datetime):
        return price_store.load_ohlcv(symbol, start, end)

    return HindsightEvaluator(ohlcv_provider=provider)
