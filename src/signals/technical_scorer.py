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


# ── Multi-Timeframe 合成 ──────────────────────────────────────


@dataclass
class MultiTfTechnicalScore:
    """MTF 合成後のスコア。

    compute_multi_tf_technical_score が返す拡張データ。
    tf_scores に各タイムフレームの個別 TechnicalScore を保持し、
    total_score / confidence / direction はそれらを weight 合成 + alignment
    bonus/減衰で導出した最終値。

    既存呼び出し側は .as_technical_score() で従来の TechnicalScore に落として
    互換インターフェースとして使える。
    """
    tf_scores: dict[str, TechnicalScore]       # {"long": ..., "medium": ..., "short": ...}
    tf_weights: dict[str, float]                # 実際に使われた weight (enabled TF のみ)
    alignment: float                             # 0.33 / 0.67 / 1.0
    raw_score: float                             # weight 合成後 (alignment 未適用)
    total_score: float                           # alignment 適用後の最終 [-1, 1]
    confidence: float                            # [0, 1]
    direction: str                               # "long" | "short" | "neutral"

    def as_technical_score(self) -> TechnicalScore:
        """既存インターフェース互換の TechnicalScore を返す。

        カテゴリ別スコアは短期 TF の値を採用 (エントリータイミング判定は
        短期が中心のため)。短期 TF がない場合は最初に見つかった TF を使う。
        """
        ref = self.tf_scores.get("short") or next(iter(self.tf_scores.values()), None)
        if ref is None:
            return TechnicalScore(
                sma_score=0.0, rsi_score=0.0, macd_score=0.0,
                ichimoku_score=0.0, bb_score=0.0, pattern_score=0.0,
                adx_factor=1.0,
                total_score=self.total_score,
                confidence=self.confidence,
                direction=self.direction,
            )
        return TechnicalScore(
            sma_score=ref.sma_score,
            rsi_score=ref.rsi_score,
            macd_score=ref.macd_score,
            ichimoku_score=ref.ichimoku_score,
            bb_score=ref.bb_score,
            pattern_score=ref.pattern_score,
            adx_factor=ref.adx_factor,
            total_score=self.total_score,
            confidence=self.confidence,
            direction=self.direction,
        )

    def format_for_prompt(self) -> str:
        """LLM プロンプト用の簡潔な MTF サマリー。"""
        lines = [
            f"[MTF] total_bias={self.total_score:+.3f}  "
            f"direction={self.direction}  confidence={self.confidence:.2f}  "
            f"alignment={self.alignment:.2f}",
        ]
        for tf_name in ("long", "medium", "short"):
            if tf_name not in self.tf_scores:
                continue
            s = self.tf_scores[tf_name]
            w = self.tf_weights.get(tf_name, 0.0)
            lines.append(
                f"  {tf_name:6s}: score={s.total_score:+.3f}  "
                f"dir={s.direction:7s}  conf={s.confidence:.2f}  (weight={w:.2f})"
            )
        return "\n".join(lines)


def _direction_sign(direction: str) -> int:
    return {"long": 1, "short": -1, "neutral": 0}.get(direction, 0)


def _alignment_score(signs: list[int]) -> float:
    """方向符号リストから alignment スコアを計算。

    全 neutral → 0.33 (不確実)
    全て同じ非 neutral → 1.0
    2/3 同方向 → 0.67
    ミックス → 0.33
    """
    non_zero = [s for s in signs if s != 0]
    if not non_zero:
        return 0.33
    if all(s == non_zero[0] for s in non_zero):
        # 全 non-neutral が同方向 (neutral が混じってもここに該当)
        return 1.0 if len(non_zero) >= 2 else 0.67
    # 方向が混在
    pos = sum(1 for s in signs if s > 0)
    neg = sum(1 for s in signs if s < 0)
    total = max(pos, neg)
    if total >= 2 and (pos == 0 or neg == 0):
        return 1.0
    if max(pos, neg) >= 2:
        return 0.67
    return 0.33


def compute_multi_tf_technical_score(
    tf_scores: dict[str, TechnicalScore],
    weights: dict[str, float],
) -> MultiTfTechnicalScore:
    """複数タイムフレームの TechnicalScore を合成する。

    Args:
        tf_scores: {"long": TechnicalScore, "medium": ..., "short": ...}
                   欠落した TF はその TF が計算できなかったことを示す。
        weights:   {"long": 0.40, "medium": 0.35, "short": 0.25} など。
                   欠落 TF は合成から除外され、残りの weight を再正規化。

    合成ロジック:
        raw_score = Σ (tf.total_score × tf.weight) / Σ weight
        alignment = 方向一致率 (0.33 / 0.67 / 1.0)
        final_score = raw_score × (0.5 + 0.5 × alignment)
                      - alignment 1.0 で 100%、0.33 で 約67%、不一致なら減衰
        confidence = avg(tf.confidence) × (0.8 + 0.2 × alignment)
    """
    present = {name: s for name, s in tf_scores.items() if s is not None}
    active_weights = {
        name: weights.get(name, 0.0) for name in present
        if weights.get(name, 0.0) > 0.0
    }
    total_weight = sum(active_weights.values())

    if total_weight <= 0.0 or not present:
        return MultiTfTechnicalScore(
            tf_scores={},
            tf_weights={},
            alignment=0.33,
            raw_score=0.0,
            total_score=0.0,
            confidence=0.5,
            direction="neutral",
        )

    # weight 再正規化した係数で合成
    raw_score = sum(
        present[name].total_score * (w / total_weight)
        for name, w in active_weights.items()
    )

    signs = [_direction_sign(present[name].direction) for name in active_weights]
    alignment = _alignment_score(signs)

    # 減衰/増幅
    final_score = raw_score * (0.5 + 0.5 * alignment)
    final_score = _clamp(final_score)

    # confidence: 各 TF confidence の重み付き平均 × alignment bonus
    weighted_conf = sum(
        present[name].confidence * (w / total_weight)
        for name, w in active_weights.items()
    )
    confidence = _clamp(weighted_conf * (0.8 + 0.2 * alignment), 0.1, 0.95)

    # 方向判定
    if final_score > 0.05:
        direction = "long"
    elif final_score < -0.05:
        direction = "short"
    else:
        direction = "neutral"

    return MultiTfTechnicalScore(
        tf_scores=dict(present),
        tf_weights=active_weights,
        alignment=alignment,
        raw_score=raw_score,
        total_score=final_score,
        confidence=confidence,
        direction=direction,
    )
