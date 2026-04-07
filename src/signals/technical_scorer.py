"""ルールベーステクニカルスコアリングエンジン。

IndicatorSummary からバイアススコアをルールで算出する。
LLM による非決定的な推論を置き換え、再現性・高速性を確保する。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.indicators import IndicatorSummary


@dataclass
class TechnicalScore:
    """テクニカルスコアの各カテゴリ値と合計を保持するデータクラス。"""

    sma_score: float       # [-1, 1]
    rsi_score: float       # [-1, 1]
    macd_score: float      # [-1, 1]
    ichimoku_score: float  # [-1, 1]
    bb_score: float        # [-1, 1]
    pattern_score: float   # [-1, 1]
    adx_factor: float      # ADX フィルター係数
    total_score: float     # [-1, 1] 重み付き合計 × ADX
    confidence: float      # [0.1, 0.95]
    direction: str         # "long" | "short" | "neutral"

    def format_for_prompt(self) -> str:
        """プロンプト埋め込み用のマルチライン文字列を返す。"""
        lines = [
            f"[テクニカルスコア] total_bias={self.total_score:+.3f}  "
            f"direction={self.direction}  confidence={self.confidence:.2f}",
            f"  SMA alignment : {self.sma_score:+.3f}",
            f"  RSI zone      : {self.rsi_score:+.3f}",
            f"  MACD momentum : {self.macd_score:+.3f}",
            f"  Ichimoku      : {self.ichimoku_score:+.3f}",
            f"  BB position   : {self.bb_score:+.3f}",
            f"  Chart pattern : {self.pattern_score:+.3f}",
            f"  ADX factor    : {self.adx_factor:.2f}",
        ]
        return "\n".join(lines)


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _lerp(a: float, b: float, t: float) -> float:
    """線形補間 (t は [0,1] にクランプ)。"""
    t = _clamp(t, 0.0, 1.0)
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# カテゴリスコア計算
# ---------------------------------------------------------------------------

def _score_sma(ind: IndicatorSummary) -> float:
    """SMA アライメントスコア (weight=0.20)。"""
    score = 0.0
    price = ind.current_price
    if price > ind.sma_20:
        score += 0.3
    else:
        score -= 0.3
    if ind.sma_20 > ind.sma_50:
        score += 0.3
    else:
        score -= 0.3
    if ind.sma_50 > ind.sma_200:
        score += 0.4
    else:
        score -= 0.4
    return _clamp(score)


def _score_rsi(ind: IndicatorSummary) -> float:
    """RSI ゾーンスコア (weight=0.15)。"""
    rsi = ind.rsi_14
    if rsi > 60:
        return _lerp(0.0, 1.0, (rsi - 60) / 20)
    elif rsi < 40:
        return -_lerp(0.0, 1.0, (40 - rsi) / 20)
    return 0.0


def _score_macd(ind: IndicatorSummary) -> float:
    """MACD モメンタムスコア (weight=0.15)。"""
    score = 0.0
    if ind.macd_histogram > 0:
        score += 0.5
    else:
        score -= 0.5
    if ind.macd_line > ind.macd_signal:
        score += 0.5
    else:
        score -= 0.5
    return _clamp(score)


def _score_ichimoku(ind: IndicatorSummary) -> float:
    """一目均衡表スコア (weight=0.25)。"""
    mapping = {
        "strong_bullish": 1.0,
        "bullish": 0.5,
        "neutral": 0.0,
        "bearish": -0.5,
        "strong_bearish": -1.0,
    }
    return mapping.get(ind.ichimoku_signal, 0.0)


def _score_bb(ind: IndicatorSummary) -> float:
    """ボリンジャーバンド位置スコア (weight=0.10)。"""
    pct_b = ind.bb_pct_b
    if pct_b > 0.8:
        return _lerp(0.0, 1.0, (pct_b - 0.8) / 0.2)
    elif pct_b < 0.2:
        return -_lerp(0.0, 1.0, (0.2 - pct_b) / 0.2)
    return 0.0


def _score_pattern(ind: IndicatorSummary) -> float:
    """チャートパターンスコア (weight=0.15)。"""
    mapping = {
        "bullish": 0.7,
        "bearish": -0.7,
        "neutral": 0.0,
    }
    return mapping.get(ind.pattern_bias, 0.0)


def _adx_factor(adx: float) -> float:
    """ADX フィルター係数。"""
    if adx >= 25:
        return 1.0
    elif adx >= 15:
        return 0.6
    else:
        return 0.3


# ---------------------------------------------------------------------------
# メイン関数
# ---------------------------------------------------------------------------

_WEIGHTS = {
    "sma": 0.20,
    "rsi": 0.15,
    "macd": 0.15,
    "ichimoku": 0.25,
    "bb": 0.10,
    "pattern": 0.15,
}
_TOTAL_WEIGHT = sum(_WEIGHTS.values())  # == 1.0


def compute_technical_score(ind: IndicatorSummary) -> TechnicalScore:
    """IndicatorSummary からルールベースで TechnicalScore を計算して返す。"""

    sma = _score_sma(ind)
    rsi = _score_rsi(ind)
    macd = _score_macd(ind)
    ichimoku = _score_ichimoku(ind)
    bb = _score_bb(ind)
    pattern = _score_pattern(ind)

    weighted_sum = (
        sma * _WEIGHTS["sma"]
        + rsi * _WEIGHTS["rsi"]
        + macd * _WEIGHTS["macd"]
        + ichimoku * _WEIGHTS["ichimoku"]
        + bb * _WEIGHTS["bb"]
        + pattern * _WEIGHTS["pattern"]
    )

    factor = _adx_factor(ind.adx_14)
    raw_total = (weighted_sum / _TOTAL_WEIGHT) * factor
    total = _clamp(raw_total)

    # --- Confidence ---
    scores = [sma, rsi, macd, ichimoku, bb, pattern]
    positives = sum(1 for s in scores if s > 0)
    negatives = sum(1 for s in scores if s < 0)
    non_neutral = positives + negatives

    if non_neutral == 0:
        agreement = 0.5
    else:
        majority = max(positives, negatives)
        agreement = majority / non_neutral

    adx = ind.adx_14
    adx_conf_adj = 0.0
    if adx > 30:
        adx_conf_adj = 0.1
    elif adx < 15:
        adx_conf_adj = -0.15

    confidence = _clamp(agreement + adx_conf_adj, 0.1, 0.95)

    # --- Direction ---
    if total > 0.05:
        direction = "long"
    elif total < -0.05:
        direction = "short"
    else:
        direction = "neutral"

    return TechnicalScore(
        sma_score=sma,
        rsi_score=rsi,
        macd_score=macd,
        ichimoku_score=ichimoku,
        bb_score=bb,
        pattern_score=pattern,
        adx_factor=factor,
        total_score=total,
        confidence=confidence,
        direction=direction,
    )
