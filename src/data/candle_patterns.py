"""チャートパターン検出モジュール。

ローソク足パターン・チャート形状・ブレイクアウト系の3カテゴリを実装。
各パターンは ChartPatternConfig の対応フラグが True のときのみ検出する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

# パターンの方向分類
_BULLISH_PATTERNS = frozenset({
    "hammer", "bullish_engulfing", "morning_star",
    "three_white_soldiers", "bullish_pin_bar",
    "double_bottom", "inv_head_shoulders",
    "ascending_triangle", "symmetrical_triangle",
    "sr_breakout_bullish",
})
_BEARISH_PATTERNS = frozenset({
    "shooting_star", "bearish_engulfing", "evening_star",
    "three_black_crows", "bearish_pin_bar",
    "double_top", "head_shoulders",
    "descending_triangle",
    "sr_breakout_bearish",
})
_NEUTRAL_PATTERNS = frozenset({
    "doji", "inside_bar", "range_bound",
    "bb_squeeze", "atr_contraction",
})


@dataclass
class PatternResult:
    detected: list[str] = field(default_factory=list)
    bullish: list[str] = field(default_factory=list)
    bearish: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)
    bias: str = "neutral"  # "bullish" | "bearish" | "neutral"


# ── ローソク足計算ヘルパー ───────────────────────────────────────────────────

def _body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _range(h: float, l: float) -> float:
    return h - l


# ── ローソク足パターン（1〜3本） ────────────────────────────────────────────

def _detect_hammer(df: pd.DataFrame) -> list[str]:
    """ハンマー: 下ヒゲ≥実体×2、上ヒゲ≤実体×0.5、直前が下降（終値が前足より低い）。"""
    results = []
    if len(df) < 3:
        return results
    for i in range(2, len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        body = _body(o, c)
        low_wick = _lower_wick(o, l, c)
        up_wick = _upper_wick(o, h, c)
        if body == 0:
            continue
        prev_c = df["Close"].iloc[i - 1]
        prior_c = df["Close"].iloc[i - 2]
        prior_downtrend = prev_c < prior_c
        if low_wick >= body * 2 and up_wick <= body * 0.5 and prior_downtrend:
            results.append(i)
    return ["hammer"] if results else []


def _detect_shooting_star(df: pd.DataFrame) -> list[str]:
    """シューティングスター: 上ヒゲ≥実体×2、下ヒゲ≤実体×0.5、直前が上昇。"""
    results = []
    if len(df) < 3:
        return results
    for i in range(2, len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        body = _body(o, c)
        up_wick = _upper_wick(o, h, c)
        low_wick = _lower_wick(o, l, c)
        if body == 0:
            continue
        prev_c = df["Close"].iloc[i - 1]
        prior_c = df["Close"].iloc[i - 2]
        prior_uptrend = prev_c > prior_c
        if up_wick >= body * 2 and low_wick <= body * 0.5 and prior_uptrend:
            results.append(i)
    return ["shooting_star"] if results else []


def _detect_engulfing(df: pd.DataFrame) -> list[str]:
    """包み足: 前足を当足の実体が完全包含かつ方向逆転。"""
    found_bullish = False
    found_bearish = False
    if len(df) < 2:
        return []
    for i in range(1, len(df)):
        o1, c1 = df["Open"].iloc[i - 1], df["Close"].iloc[i - 1]
        o2, c2 = df["Open"].iloc[i], df["Close"].iloc[i]
        prev_bull = c1 > o1
        prev_bear = c1 < o1
        prev_body_top = max(o1, c1)
        prev_body_bot = min(o1, c1)
        curr_body_top = max(o2, c2)
        curr_body_bot = min(o2, c2)
        # 強気エングルフィング: 前足陰線、当足陽線で完全包含
        if prev_bear and c2 > o2 and curr_body_bot < prev_body_bot and curr_body_top > prev_body_top:
            found_bullish = True
        # 弱気エングルフィング: 前足陽線、当足陰線で完全包含
        if prev_bull and c2 < o2 and curr_body_bot < prev_body_bot and curr_body_top > prev_body_top:
            found_bearish = True
    patterns = []
    if found_bullish:
        patterns.append("bullish_engulfing")
    if found_bearish:
        patterns.append("bearish_engulfing")
    return patterns


def _detect_doji(df: pd.DataFrame) -> list[str]:
    """十字線: 実体≤レンジの10%。"""
    if len(df) < 1:
        return []
    # 直近3本のいずれかで判定
    for i in range(max(0, len(df) - 3), len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        r = _range(h, l)
        if r == 0:
            continue
        if _body(o, c) / r <= 0.10:
            return ["doji"]
    return []


def _detect_morning_evening_star(df: pd.DataFrame) -> list[str]:
    """モーニング/イブニングスター: 3本パターン。

    classical definition:
      day1: 大きめの反対方向ロウソク (bearish for morning / bullish for evening)
      day2: 小実体 (body2 <= body1 * 0.5)
      day3: day1 と逆方向の大きめロウソク (body3 >= body1 * 0.5)
            かつ day1 の中点を越えて終値する
    """
    found_morning = False
    found_evening = False
    if len(df) < 3:
        return []
    for i in range(2, len(df)):
        o1, c1 = df["Open"].iloc[i - 2], df["Close"].iloc[i - 2]
        o2, c2 = df["Open"].iloc[i - 1], df["Close"].iloc[i - 1]
        o3, c3 = df["Open"].iloc[i], df["Close"].iloc[i]
        body1 = _body(o1, c1)
        body2 = _body(o2, c2)
        body3 = _body(o3, c3)
        # モーニングスター: 陰線 → 小実体 → 陽線 (day3 は大きめの実体が必要)
        if (c1 < o1 and body2 <= body1 * 0.5
                and c3 > o3 and body3 >= body1 * 0.5
                and c3 > (o1 + c1) / 2):
            found_morning = True
        # イブニングスター: 陽線 → 小実体 → 陰線 (day3 は大きめの実体が必要)
        if (c1 > o1 and body2 <= body1 * 0.5
                and c3 < o3 and body3 >= body1 * 0.5
                and c3 < (o1 + c1) / 2):
            found_evening = True
    patterns = []
    if found_morning:
        patterns.append("morning_star")
    if found_evening:
        patterns.append("evening_star")
    return patterns


def _detect_three_soldiers_crows(df: pd.DataFrame) -> list[str]:
    """三白兵/三黒烏: 連続3本の同方向足で各足が前足の中央以上/以下で寄付き。"""
    found_soldiers = False
    found_crows = False
    if len(df) < 3:
        return []
    for i in range(2, len(df)):
        o1, c1 = df["Open"].iloc[i - 2], df["Close"].iloc[i - 2]
        o2, c2 = df["Open"].iloc[i - 1], df["Close"].iloc[i - 1]
        o3, c3 = df["Open"].iloc[i], df["Close"].iloc[i]
        # 三白兵: 3本とも陽線、各足が前足中央以上で始まり前足高値付近で終わる
        if (c1 > o1 and c2 > o2 and c3 > o3
                and o2 >= (o1 + c1) / 2 and c2 > c1
                and o3 >= (o2 + c2) / 2 and c3 > c2):
            found_soldiers = True
        # 三黒烏: 3本とも陰線、各足が前足中央以下で始まり前足安値付近で終わる
        if (c1 < o1 and c2 < o2 and c3 < o3
                and o2 <= (o1 + c1) / 2 and c2 < c1
                and o3 <= (o2 + c2) / 2 and c3 < c2):
            found_crows = True
    patterns = []
    if found_soldiers:
        patterns.append("three_white_soldiers")
    if found_crows:
        patterns.append("three_black_crows")
    return patterns


def _detect_pin_bar(df: pd.DataFrame) -> list[str]:
    """ピンバー: 最長ヒゲ≥実体×3。方向は最長ヒゲ側の逆。"""
    found_bullish = False
    found_bearish = False
    if len(df) < 1:
        return []
    for i in range(max(0, len(df) - 3), len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        body = _body(o, c)
        up_wick = _upper_wick(o, h, c)
        low_wick = _lower_wick(o, l, c)
        if body == 0:
            continue
        # 下ヒゲピンバー（強気）: 下ヒゲが上ヒゲより長く≥実体×3
        if low_wick >= body * 3 and low_wick > up_wick:
            found_bullish = True
        # 上ヒゲピンバー（弱気）: 上ヒゲが下ヒゲより長く≥実体×3
        if up_wick >= body * 3 and up_wick > low_wick:
            found_bearish = True
    patterns = []
    if found_bullish:
        patterns.append("bullish_pin_bar")
    if found_bearish:
        patterns.append("bearish_pin_bar")
    return patterns


def _detect_inside_bar(df: pd.DataFrame) -> list[str]:
    """インサイドバー: 当足の高値・安値が前足の範囲内に完全収まる。"""
    if len(df) < 2:
        return []
    for i in range(1, len(df)):
        h1, l1 = df["High"].iloc[i - 1], df["Low"].iloc[i - 1]
        h2, l2 = df["High"].iloc[i], df["Low"].iloc[i]
        if h2 <= h1 and l2 >= l1:
            return ["inside_bar"]
    return []


# ── チャート形状パターン ────────────────────────────────────────────────────

def _get_swing_points(df: pd.DataFrame) -> tuple[list[float], list[float]]:
    """直近30本のスイング高値・安値を返す。"""
    close = df["Close"].iloc[-30:]
    highs, lows = [], []
    for i in range(1, len(close) - 1):
        if close.iloc[i] > close.iloc[i - 1] and close.iloc[i] > close.iloc[i + 1]:
            highs.append(float(close.iloc[i]))
        if close.iloc[i] < close.iloc[i - 1] and close.iloc[i] < close.iloc[i + 1]:
            lows.append(float(close.iloc[i]))
    return highs, lows


def _detect_double_top_bottom(df: pd.DataFrame) -> list[str]:
    """ダブルトップ/ダブルボトム: スイング高値/安値2本が±0.3%以内。"""
    highs, lows = _get_swing_points(df)
    patterns = []
    # ダブルトップ
    if len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        if h1 > 0 and abs(h1 - h2) / h1 <= 0.003:
            patterns.append("double_top")
    # ダブルボトム
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        if l1 > 0 and abs(l1 - l2) / l1 <= 0.003:
            patterns.append("double_bottom")
    return patterns


def _detect_head_shoulders(df: pd.DataFrame) -> list[str]:
    """ヘッド＆ショルダー: 3スイング高値で中央が最高値（逆は最安値）。"""
    highs, lows = _get_swing_points(df)
    patterns = []
    # 通常ヘッド＆ショルダー（天井）
    if len(highs) >= 3:
        l_sh, head, r_sh = highs[-3], highs[-2], highs[-1]
        if head > l_sh and head > r_sh and abs(l_sh - r_sh) / head <= 0.05:
            patterns.append("head_shoulders")
    # 逆ヘッド＆ショルダー（底）
    if len(lows) >= 3:
        l_sh, head, r_sh = lows[-3], lows[-2], lows[-1]
        if head < l_sh and head < r_sh and l_sh > 0 and abs(l_sh - r_sh) / l_sh <= 0.05:
            patterns.append("inv_head_shoulders")
    return patterns


def _detect_triangle(df: pd.DataFrame) -> list[str]:
    """三角保ち合い: 高値切り下がり・安値切り上がりの組み合わせで判定。"""
    highs, lows = _get_swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return []
    high_falling = highs[-1] < highs[-2]  # 高値切り下がり
    low_rising = lows[-1] > lows[-2]      # 安値切り上がり
    high_flat = abs(highs[-1] - highs[-2]) / highs[-2] <= 0.003 if highs[-2] > 0 else False
    low_flat = abs(lows[-1] - lows[-2]) / lows[-2] <= 0.003 if lows[-2] > 0 else False

    if high_falling and low_rising:
        return ["symmetrical_triangle"]
    if high_falling and low_flat:
        return ["descending_triangle"]
    if high_flat and low_rising:
        return ["ascending_triangle"]
    return []


def _detect_range_bound(df: pd.DataFrame, atr: float) -> list[str]:
    """レンジ相場: 直近20本の高値・安値の変動幅がATR×2以内。"""
    if len(df) < 20 or atr <= 0:
        return []
    recent = df.iloc[-20:]
    price_range = float(recent["High"].max() - recent["Low"].min())
    if price_range <= atr * 2:
        return ["range_bound"]
    return []


# ── ブレイクアウト系 ────────────────────────────────────────────────────────

def _detect_bb_squeeze(bb_upper: float, bb_lower: float, sma_20: float) -> list[str]:
    """BBスクイーズ: バンド幅/SMA20が2%以下。"""
    if sma_20 <= 0:
        return []
    band_width = (bb_upper - bb_lower) / sma_20
    if band_width <= 0.02:
        return ["bb_squeeze"]
    return []


def _detect_atr_contraction(df: pd.DataFrame, current_atr: float) -> list[str]:
    """ATR収縮: 現在ATRが直近20本の平均TRの70%以下。

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    Wilder 1978 の正式定義 — 前バーとのギャップを考慮する。
    """
    if len(df) < 21 or current_atr <= 0:
        return []

    import numpy as np

    high = df["High"].iloc[-20:].to_numpy()
    low = df["Low"].iloc[-20:].to_numpy()
    close_prev = df["Close"].iloc[-21:-1].to_numpy()

    tr = np.maximum.reduce([
        high - low,
        np.abs(high - close_prev),
        np.abs(low - close_prev),
    ])
    avg_tr = float(tr.mean())
    if avg_tr > 0 and current_atr <= avg_tr * 0.7:
        return ["atr_contraction"]
    return []


def _detect_sr_breakout(df: pd.DataFrame) -> list[str]:
    """S/Rブレイク: 当足の実体が直近5本の高値/安値を突破。"""
    if len(df) < 6:
        return []
    recent5 = df.iloc[-6:-1]
    resist = float(recent5["High"].max())
    support = float(recent5["Low"].min())
    curr_o = float(df["Open"].iloc[-1])
    curr_c = float(df["Close"].iloc[-1])
    body_top = max(curr_o, curr_c)
    body_bot = min(curr_o, curr_c)
    patterns = []
    if body_top > resist:
        patterns.append("sr_breakout_bullish")
    if body_bot < support:
        patterns.append("sr_breakout_bearish")
    return patterns


# ── メインエントリ ──────────────────────────────────────────────────────────

def detect_patterns(
    df: pd.DataFrame,
    cfg,  # ChartPatternConfig
    atr: float = 0.0,
    bb_upper: float = 0.0,
    bb_lower: float = 0.0,
    sma_20: float = 0.0,
) -> PatternResult:
    """設定に従ってチャートパターンを検出し PatternResult を返す。"""
    detected: list[str] = []

    if len(df) < 3:
        return PatternResult()

    # ── ローソク足パターン ────────────────────────────
    if cfg.hammer:
        detected.extend(_detect_hammer(df))
    if cfg.shooting_star:
        detected.extend(_detect_shooting_star(df))
    if cfg.engulfing:
        detected.extend(_detect_engulfing(df))
    if cfg.doji:
        detected.extend(_detect_doji(df))
    if cfg.morning_evening_star:
        detected.extend(_detect_morning_evening_star(df))
    if cfg.three_soldiers_crows:
        detected.extend(_detect_three_soldiers_crows(df))
    if cfg.pin_bar:
        detected.extend(_detect_pin_bar(df))
    if cfg.inside_bar:
        detected.extend(_detect_inside_bar(df))

    # ── チャート形状パターン ──────────────────────────
    if cfg.double_top_bottom:
        detected.extend(_detect_double_top_bottom(df))
    if cfg.head_shoulders:
        detected.extend(_detect_head_shoulders(df))
    if cfg.triangle:
        detected.extend(_detect_triangle(df))
    if cfg.range_bound:
        detected.extend(_detect_range_bound(df, atr))

    # ── ブレイクアウト系 ──────────────────────────────
    if cfg.bb_squeeze:
        detected.extend(_detect_bb_squeeze(bb_upper, bb_lower, sma_20))
    if cfg.atr_contraction:
        detected.extend(_detect_atr_contraction(df, atr))
    if cfg.sr_breakout:
        detected.extend(_detect_sr_breakout(df))

    # 重複除去・方向分類
    detected = list(dict.fromkeys(detected))  # 順序保持で重複削除
    bullish = [p for p in detected if p in _BULLISH_PATTERNS]
    bearish = [p for p in detected if p in _BEARISH_PATTERNS]
    neutral = [p for p in detected if p in _NEUTRAL_PATTERNS]

    if len(bullish) > len(bearish):
        bias = "bullish"
    elif len(bearish) > len(bullish):
        bias = "bearish"
    else:
        bias = "neutral"

    if detected:
        logger.debug(f"Chart patterns detected: {detected} (bias={bias})")

    return PatternResult(
        detected=detected,
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
        bias=bias,
    )
