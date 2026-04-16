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
#
# 【通貨非依存設計】
# PnL 絶対値 (500 JPY など) を直接 hardcode しないこと。口座通貨やロットサイズが
# 変わると破綻する (例: EURUSD 5000 通貨 ≠ USDJPY 5000 通貨)。
# 代わりに 1R = risk_per_trade × account_balance (= 想定最大損失/トレード) を基準に
# 比率で判定する。assign_flag() の呼び出し側で risk_budget を渡す。
_NOISE_R_RATIO = 0.10               # |pnl| < 0.10R は NOISE (リスクの 10% 未満しか動いていない)
_CONF_MISS_CONFIDENCE = 0.75        # confidence がこの値以上 + 大損で CONF_MISS
_CONF_MISS_R_RATIO = 0.50           # |pnl| > 0.50R かつ高 conf で CONF_MISS
_TIGHT_TP_MFE_RATIO = 1.5           # MFE > realized * この比率で TIGHT_TP
_LATE_EXIT_INTRA_MFE_RATIO = 1.5    # エントリー中 MFE > realized * この比率で LATE_EXIT
_COUNTERFACTUAL_ATR_STEPS = (0.5, 1.0)  # TP wider, SL tighter に使う ATR 倍率

_BUY_DIRECTIONS = frozenset({"buy", "bullish", "long"})
_SELL_DIRECTIONS = frozenset({"sell", "bearish", "short"})


def _normalize_direction(direction: str) -> str:
    """direction を 'buy' / 'sell' に正規化する。

    trading.py は 'bullish'/'bearish' を使い、audit テストは 'buy'/'sell' を
    使うため、両方を受け入れて内部では 'buy'/'sell' に統一する。
    未知の値は ValueError を投げる。
    """
    d = (direction or "").lower().strip()
    if d in _BUY_DIRECTIONS:
        return "buy"
    if d in _SELL_DIRECTIONS:
        return "sell"
    raise ValueError(
        f"direction must be one of buy/bullish/long/sell/bearish/short, got {direction!r}"
    )


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

    direction = _normalize_direction(direction)
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

    direction = _normalize_direction(direction)
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


def compute_vol_percentile(
    atr_pct: float,
    distribution: list[float],
) -> float | None:
    """atr_pct が分布の何 percentile にあるかを返す (0-100)。

    Args:
        atr_pct: エントリー時の ATR / price 比 (例: 0.005 = 0.5%)
        distribution: 過去 90 日分の atr_pct サンプル
    Returns:
        0.0〜100.0 の percentile、分布が空なら None
    """
    if not distribution:
        return None
    sorted_d = sorted(distribution)
    n = len(sorted_d)
    # 境界処理
    if atr_pct < sorted_d[0]:
        return 0.0
    if atr_pct > sorted_d[-1]:
        return 100.0
    # atr_pct 未満の要素数 + 等値の半分
    below = sum(1 for x in sorted_d if x < atr_pct)
    equal = sum(1 for x in sorted_d if x == atr_pct)
    return (below + equal / 2) / n * 100


def assign_flag(
    realized_pnl: float,
    signal_confidence: float,
    close_reason: str,
    ph: PostHocResult,
    cf: Counterfactuals,
    risk_budget: float = 0.0,
) -> str:
    """rule-based の flag を返す。

    判定順:
    1. NOISE: |pnl| < NOISE_R_RATIO × risk_budget (リスク予算の一部しか動いていない)
    2. INSUFFICIENT_DATA: post-close OHLCV なし
    3. 勝ち: CLEAN_WIN / TIGHT_TP / LATE_EXIT
    4. 敗け: SL_RECOVER / CONF_MISS / CLEAN_LOSS
    5. それ以外: NEUTRAL

    Args:
        risk_budget: 1R = risk_per_trade × account_balance。0 なら NOISE/CONF_MISS
            の絶対値判定を skip (後方互換: 閾値なしで動作)。
    """
    # 通貨非依存: |pnl| を risk_budget (1R) に対する比率で評価する
    if risk_budget > 0 and abs(realized_pnl) < _NOISE_R_RATIO * risk_budget:
        return "NOISE"
    if not ph.has_post_close_data:
        return "INSUFFICIENT_DATA"

    win = realized_pnl > 0
    tp_hit = close_reason == "take_profit"
    # stop_loss / emergency_stop はどちらも逆行による決済 → SL 系として統一判定
    sl_hit = close_reason in ("stop_loss", "emergency_stop")
    conf_high = signal_confidence >= _CONF_MISS_CONFIDENCE

    if win:
        if tp_hit and ph.mfe_after_close_24h < 0.5 * abs(realized_pnl):
            return "CLEAN_WIN"
        if ph.mfe_after_close_24h > _TIGHT_TP_MFE_RATIO * abs(realized_pnl):
            return "TIGHT_TP"
        if not tp_hit and ph.mfe_during_trade > _LATE_EXIT_INTRA_MFE_RATIO * abs(realized_pnl):
            return "LATE_EXIT"
        return "CLEAN_WIN"

    # loss
    if sl_hit and cf.tighter_sl_would_recover:
        return "SL_RECOVER"
    conf_miss_threshold = _CONF_MISS_R_RATIO * risk_budget if risk_budget > 0 else 0.0
    if conf_high and abs(realized_pnl) > conf_miss_threshold:
        return "CONF_MISS"
    if sl_hit:
        return "CLEAN_LOSS"
    return "NEUTRAL"


def build_atr_pct_distribution(ohlcv_df, atr_period: int = 14) -> list[float]:
    """過去 OHLCV から ATR% (= ATR / Close) の分布を作る。

    audit での vol_percentile 計算のベース分布として使用する。
    """
    import pandas as pd

    if ohlcv_df is None or len(ohlcv_df) < atr_period + 1:
        return []

    high = ohlcv_df["High"]
    low = ohlcv_df["Low"]
    close = ohlcv_df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()
    atr_pct = (atr / close).dropna()
    return [float(v) for v in atr_pct.tolist() if v > 0]
