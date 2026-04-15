"""Rule-based post-hoc 分析モジュール。

audit でトレードの結果を再評価する:
- MFE / MAE (Max Favorable/Adverse Excursion)
- counterfactuals (wider TP / tighter SL の仮想結果)
- vol percentile (ペア別 90 日分布でのエントリー時ボラ位置)
- flag 判定 (CLEAN_WIN / TIGHT_TP / LATE_EXIT / CLEAN_LOSS / CONF_MISS / SL_RECOVER / NOISE)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# flag 判定用の閾値 (tune 可能な定数として module 先頭に置く)
_NOISE_PNL_THRESHOLD = 500.0        # 絶対値がこの値未満は NOISE (JPY ベース想定)
_CONF_MISS_CONFIDENCE = 0.75        # confidence がこの値以上 + 大損で CONF_MISS
_CONF_MISS_PNL_MIN = 1000.0         # CONF_MISS 判定に必要な最低損失絶対値
_TIGHT_TP_MFE_RATIO = 1.5           # MFE > realized * この比率で TIGHT_TP
_LATE_EXIT_INTRA_MFE_RATIO = 1.5    # エントリー中 MFE > realized * この比率で LATE_EXIT
_COUNTERFACTUAL_ATR_STEPS = (0.5, 1.0)  # TP wider, SL tighter に使う ATR 倍率


@dataclass
class PostHocResult:
    """トレードの post-hoc 計算結果。"""
    mfe_during_trade: float      # エントリー〜決済の最大順行幅 (pnl 換算)
    mae_during_trade: float      # 同、最大逆行幅 (pnl 換算、負値)
    mfe_after_close_24h: float   # 決済後 24h の最大順行幅 (方向を維持した場合)
    mae_after_close_24h: float   # 同、最大逆行幅
    duration_seconds: float
    has_post_close_data: bool    # False なら INSUFFICIENT_DATA


@dataclass
class Counterfactuals:
    """仮想 TP/SL での結果。"""
    tp_plus_0_5_atr_hit: bool
    tp_plus_0_5_atr_pnl: float
    tp_plus_1_0_atr_hit: bool
    tp_plus_1_0_atr_pnl: float
    sl_minus_0_5_atr_hit: bool   # SL を 0.5 ATR タイトに
    sl_minus_0_5_atr_pnl: float
    tighter_sl_would_recover: bool  # SL タイト化でも最終的に利益に戻ったか


@dataclass
class LessonCandidate:
    """LLM が生成した改善ルール候補。"""
    rule_text: str               # "[CONDITION] → [ACTION]"
    rationale: str               # なぜこの trade が rule を正当化するか
    applicability: str           # 適用条件 / scope
    generated_at: datetime = field(default_factory=datetime.now)
    hint_used: str = ""          # 再生成時に使ったヒント (初回は空)


@dataclass
class TradeReview:
    """1 トレードの audit 情報を全てまとめたもの。"""
    session_id: str
    pair: str
    direction: str
    entry_price: float
    close_price: float
    close_reason: str
    realized_pnl: float
    signal_confidence: float
    signal_score: float
    opened_at: datetime
    closed_at: datetime
    analysis_summary: str
    macro_context: str
    reflection_text: str
    atr_value: float
    stop_loss: float
    take_profit: float
    post_hoc: PostHocResult
    counterfactuals: Counterfactuals
    vol_percentile: float | None  # None = ペアの 90d データ不足
    flag: str
    lesson_candidates: list[LessonCandidate] = field(default_factory=list)
    accepted_lessons: list[LessonCandidate] = field(default_factory=list)


def compute_mfe_mae(
    direction: str,
    entry_price: float,
    close_price: float,
    position_size: float,
    opened_at: datetime,
    closed_at: datetime,
    ohlcv_df,  # pandas.DataFrame (index=datetime)
    post_close_hours: int = 24,
) -> PostHocResult:
    """トレード期間と決済後 24h の MFE/MAE を計算する。

    MFE/MAE は「方向に順行した場合の最大利益 / 損失」を pnl 相当で返す。
    OHLCV の High/Low を参照し、direction に応じて符号を調整する。
    """
    import pandas as pd

    if direction not in ("buy", "sell"):
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction!r}")

    direction_sign = 1 if direction == "buy" else -1
    duration_seconds = (closed_at - opened_at).total_seconds()

    if ohlcv_df is None or ohlcv_df.empty:
        return PostHocResult(
            mfe_during_trade=0.0, mae_during_trade=0.0,
            mfe_after_close_24h=0.0, mae_after_close_24h=0.0,
            duration_seconds=duration_seconds, has_post_close_data=False,
        )

    # 重複 timestamp (pd.concat などで発生) を除去して duplicate index 問題を防ぐ。
    # 最後の出現を残すことで「close バーは intra 期間の最終バー」の意味を維持。
    ohlcv_df = ohlcv_df[~ohlcv_df.index.duplicated(keep="last")]

    opened_ts = pd.Timestamp(opened_at)
    closed_ts = pd.Timestamp(closed_at)

    intra = ohlcv_df[(ohlcv_df.index >= opened_ts) & (ohlcv_df.index <= closed_ts)]
    if intra.empty:
        mfe_intra = 0.0
        mae_intra = 0.0
    else:
        if direction == "buy":
            best = float(intra["High"].max())
            worst = float(intra["Low"].min())
        else:
            best = float(intra["Low"].min())
            worst = float(intra["High"].max())
        mfe_intra = (best - entry_price) * direction_sign * position_size
        mae_intra = (worst - entry_price) * direction_sign * position_size
        # MAE must be non-positive per contract; clamp to 0 when price never moved adverse
        mae_intra = min(mae_intra, 0.0)

    post_end = closed_ts + pd.Timedelta(hours=post_close_hours)
    post = ohlcv_df[(ohlcv_df.index > closed_ts) & (ohlcv_df.index <= post_end)]
    has_post = not post.empty
    if has_post:
        if direction == "buy":
            best_post = float(post["High"].max())
            worst_post = float(post["Low"].min())
        else:
            best_post = float(post["Low"].min())
            worst_post = float(post["High"].max())
        mfe_after = (best_post - close_price) * direction_sign * position_size
        mae_after = (worst_post - close_price) * direction_sign * position_size
        mae_after = min(mae_after, 0.0)
    else:
        mfe_after = 0.0
        mae_after = 0.0

    return PostHocResult(
        mfe_during_trade=mfe_intra,
        mae_during_trade=mae_intra,
        mfe_after_close_24h=mfe_after,
        mae_after_close_24h=mae_after,
        duration_seconds=duration_seconds,
        has_post_close_data=has_post,
    )


def compute_counterfactuals(
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    position_size: float,
    atr_value: float,
    opened_at: datetime,
    closed_at: datetime,
    ohlcv_df,
) -> Counterfactuals:
    """仮想 TP/SL で決済していた場合の結果を計算する。

    - tp_plus_0_5_atr / tp_plus_1_0_atr: TP を ATR * 0.5 / 1.0 広げた場合
    - sl_minus_0_5_atr: SL を ATR * 0.5 タイト (不利方向に狭める) にした場合
    - tighter_sl_would_recover: SL タイト化でヒットした後に最終的に順行方向へ戻ったか
    """
    import pandas as pd

    if direction not in ("buy", "sell"):
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction!r}")

    direction_sign = 1 if direction == "buy" else -1
    if ohlcv_df is None or ohlcv_df.empty:
        return Counterfactuals(
            tp_plus_0_5_atr_hit=False, tp_plus_0_5_atr_pnl=0.0,
            tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=0.0,
            sl_minus_0_5_atr_hit=False, sl_minus_0_5_atr_pnl=0.0,
            tighter_sl_would_recover=False,
        )

    # 重複 index を除去 (concat などで発生しうる)
    ohlcv_df = ohlcv_df[~ohlcv_df.index.duplicated(keep="last")]

    opened_ts = pd.Timestamp(opened_at)
    closed_ts = pd.Timestamp(closed_at)
    intra = ohlcv_df[(ohlcv_df.index >= opened_ts) & (ohlcv_df.index <= closed_ts)]
    if intra.empty:
        return Counterfactuals(
            tp_plus_0_5_atr_hit=False, tp_plus_0_5_atr_pnl=0.0,
            tp_plus_1_0_atr_hit=False, tp_plus_1_0_atr_pnl=0.0,
            sl_minus_0_5_atr_hit=False, sl_minus_0_5_atr_pnl=0.0,
            tighter_sl_would_recover=False,
        )

    highs = intra["High"]
    lows = intra["Low"]

    # wider TP (順行方向に広げる)
    def tp_hit(offset: float) -> tuple[bool, float]:
        new_tp = take_profit + (offset * direction_sign)
        if direction == "buy":
            hit = (highs >= new_tp).any()
        else:
            hit = (lows <= new_tp).any()
        pnl = (new_tp - entry_price) * direction_sign * position_size if hit else 0.0
        return bool(hit), pnl

    tp05_hit, tp05_pnl = tp_hit(0.5 * atr_value)
    tp10_hit, tp10_pnl = tp_hit(1.0 * atr_value)

    # tighter SL (逆行方向に狭める = entry に近づける)
    new_sl = stop_loss + (0.5 * atr_value * direction_sign)  # entry に近づく
    if direction == "buy":
        sl_hit_mask = (lows <= new_sl)
    else:
        sl_hit_mask = (highs >= new_sl)
    sl_hit_time = None
    if sl_hit_mask.any():
        sl_hit_time = sl_hit_mask.idxmax()  # 最初に hit した時刻
    sl_hit_val = bool(sl_hit_mask.any())
    sl_pnl = (new_sl - entry_price) * direction_sign * position_size if sl_hit_val else 0.0

    # tighter_sl_would_recover: SL hit 後に順行方向へ戻ったか (entry 以上に戻ったか)
    recover = False
    if sl_hit_val and sl_hit_time is not None:
        after_hit = intra[intra.index > sl_hit_time]
        if not after_hit.empty:
            if direction == "buy":
                recover = bool((after_hit["High"] >= entry_price).any())
            else:
                recover = bool((after_hit["Low"] <= entry_price).any())

    return Counterfactuals(
        tp_plus_0_5_atr_hit=tp05_hit,
        tp_plus_0_5_atr_pnl=tp05_pnl,
        tp_plus_1_0_atr_hit=tp10_hit,
        tp_plus_1_0_atr_pnl=tp10_pnl,
        sl_minus_0_5_atr_hit=sl_hit_val,
        sl_minus_0_5_atr_pnl=sl_pnl,
        tighter_sl_would_recover=recover,
    )
