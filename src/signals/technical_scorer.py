"""ルールベーステクニカルスコアリングエンジン。

IndicatorSummary からバイアススコアをルールで算出する。
LLM による非決定的な推論を置き換え、再現性・高速性を確保する。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.data.indicators import IndicatorSummary

if TYPE_CHECKING:
    from src.config import ChartPatternConfig, IndicatorToggleConfig


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


def _any_pattern_enabled(pattern_cfg: "ChartPatternConfig | None") -> bool:
    """pattern_cfg が None ならデフォルト (有効) 扱い。任意の 1 つでも True なら有効。"""
    if pattern_cfg is None:
        return True
    # ChartPatternConfig の全ブール属性を走査
    return any(
        getattr(pattern_cfg, name) is True
        for name in pattern_cfg.__dataclass_fields__
        if isinstance(getattr(pattern_cfg, name), bool)
    )


def compute_technical_score(
    ind: IndicatorSummary,
    indicator_cfg: "IndicatorToggleConfig | None" = None,
    pattern_cfg: "ChartPatternConfig | None" = None,
) -> TechnicalScore:
    """IndicatorSummary からルールベースで TechnicalScore を計算して返す。

    indicator_cfg / pattern_cfg を指定すると、無効化されたカテゴリはスコア 0
    として扱い、残りの weight を動的に再正規化する。これにより
    `compute_indicators` が disabled 指標に対して返す 0.0 デフォルトが
    人工的に bearish 側に倒す挙動を防ぐ。

    cfg=None (省略時) は全指標有効と同等の振る舞いで、既存呼び出しと後方互換。
    """

    # --- カテゴリごとの enable 判定 ---
    sma_on = indicator_cfg is None or indicator_cfg.moving_averages
    rsi_on = indicator_cfg is None or indicator_cfg.rsi
    macd_on = indicator_cfg is None or indicator_cfg.macd
    bb_on = indicator_cfg is None or indicator_cfg.bollinger_bands
    ichimoku_on = indicator_cfg is None or indicator_cfg.ichimoku
    pattern_on = _any_pattern_enabled(pattern_cfg)

    # --- カテゴリスコア (無効カテゴリは 0) ---
    sma = _score_sma(ind) if sma_on else 0.0
    rsi = _score_rsi(ind) if rsi_on else 0.0
    macd = _score_macd(ind) if macd_on else 0.0
    ichimoku = _score_ichimoku(ind) if ichimoku_on else 0.0
    bb = _score_bb(ind) if bb_on else 0.0
    pattern = _score_pattern(ind) if pattern_on else 0.0

    # --- 動的 weight 再正規化 (無効カテゴリの weight を除外) ---
    enabled_weights = {
        "sma": _WEIGHTS["sma"] if sma_on else 0.0,
        "rsi": _WEIGHTS["rsi"] if rsi_on else 0.0,
        "macd": _WEIGHTS["macd"] if macd_on else 0.0,
        "ichimoku": _WEIGHTS["ichimoku"] if ichimoku_on else 0.0,
        "bb": _WEIGHTS["bb"] if bb_on else 0.0,
        "pattern": _WEIGHTS["pattern"] if pattern_on else 0.0,
    }
    total_weight = sum(enabled_weights.values())

    if total_weight <= 0.0:
        # 全指標 disabled → neutral
        return TechnicalScore(
            sma_score=0.0,
            rsi_score=0.0,
            macd_score=0.0,
            ichimoku_score=0.0,
            bb_score=0.0,
            pattern_score=0.0,
            adx_factor=_adx_factor(ind.adx_14),
            total_score=0.0,
            confidence=0.5,
            direction="neutral",
        )

    weighted_sum = (
        sma * enabled_weights["sma"]
        + rsi * enabled_weights["rsi"]
        + macd * enabled_weights["macd"]
        + ichimoku * enabled_weights["ichimoku"]
        + bb * enabled_weights["bb"]
        + pattern * enabled_weights["pattern"]
    )

    factor = _adx_factor(ind.adx_14)
    raw_total = (weighted_sum / total_weight) * factor
    total = _clamp(raw_total)

    # --- Confidence — 有効カテゴリだけで agreement を計算 ---
    enabled_scores = [
        s for s, on in [
            (sma, sma_on), (rsi, rsi_on), (macd, macd_on),
            (ichimoku, ichimoku_on), (bb, bb_on), (pattern, pattern_on),
        ] if on
    ]
    positives = sum(1 for s in enabled_scores if s > 0)
    negatives = sum(1 for s in enabled_scores if s < 0)
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
