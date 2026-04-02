# src/signals/rag_adjustment.py
"""方向別RAG検索結果からシグナルスコアの補正値を算出する。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RagAdjustmentConfig:
    enabled: bool = True
    max_adjustment: float = 0.15
    min_hits: int = 2
    search_top_n: int = 5
    same_direction_weight: float = 0.10
    opposite_direction_weight: float = 0.10
    trade_weight_multiplier: float = 1.0
    forecast_weight_multiplier: float = 0.5
    hold_weight_multiplier: float = 0.3


def _session_type_weight(session_type: str, config: RagAdjustmentConfig) -> float:
    """session_typeに応じた重みを返す。"""
    weights = {
        "trade": config.trade_weight_multiplier,
        "forecast": config.forecast_weight_multiplier,
        "hold": config.hold_weight_multiplier,
    }
    return weights.get(session_type, 0.5)


def compute_rag_adjustment(
    combined_score: float,
    same_direction_hits: list[dict],
    opposite_direction_hits: list[dict],
    config: RagAdjustmentConfig,
) -> float:
    """方向別RAG検索結果からスコア補正値を算出する。

    Args:
        combined_score: 現在のcombined_score（正=bullish, 負=bearish）
        same_direction_hits: 同方向コレクションの検索結果
        opposite_direction_hits: 対向コレクションの検索結果
        config: 補正設定

    Returns:
        rag_adjustment: -max_adjustment 〜 +max_adjustment の補正値。
        combined_score が正の場合、正の補正値 = bullish強化、負 = bullish弱化。
        combined_score が負の場合、負の補正値 = bearish強化、正 = bearish弱化。
    """
    if not config.enabled:
        return 0.0

    valid_same = [h for h in same_direction_hits if h.get("metadata", {}).get("phase") == "complete"]
    valid_opposite = [h for h in opposite_direction_hits if h.get("metadata", {}).get("phase") == "complete"]

    total_valid = len(valid_same) + len(valid_opposite)
    if total_valid < config.min_hits:
        return 0.0

    # 1. 同方向コレクション: 重み付き勝率 → 信頼度
    same_factor = 0.0
    if valid_same:
        weighted_wins = 0.0
        weighted_total = 0.0
        for h in valid_same:
            meta = h.get("metadata", {})
            w = _session_type_weight(meta.get("session_type", "trade"), config)
            weighted_total += w
            if meta.get("outcome") == "win":
                weighted_wins += w
        if weighted_total > 0:
            win_rate = weighted_wins / weighted_total
            # avg_weight normalizes influence by session type so that lower-quality
            # session types (forecast, hold) produce a smaller adjustment even when
            # win_rate is identical to a trade-only set.
            avg_weight = weighted_total / len(valid_same)
            same_factor = (win_rate - 0.5) * config.same_direction_weight * avg_weight

    # 2. 対向コレクション: 類似度が高いほど反転リスク
    opposite_factor = 0.0
    if valid_opposite:
        similarities = []
        for h in valid_opposite:
            dist = h.get("distance")
            if dist is not None:
                similarities.append(max(0.0, 1.0 - dist))
        if similarities:
            avg_similarity = sum(similarities) / len(similarities)
            opposite_factor = -avg_similarity * config.opposite_direction_weight

    # 3. 合算
    adjustment = same_factor + opposite_factor

    # 4. bearishの場合は符号反転
    if combined_score < 0:
        adjustment = -adjustment

    # 5. クランプ
    adjustment = max(-config.max_adjustment, min(config.max_adjustment, adjustment))

    logger.info(
        f"RAG Adjustment: combined={combined_score:+.3f} adj={adjustment:+.4f} "
        f"(same: {len(valid_same)} hits, factor={same_factor:+.4f} | "
        f"opposite: {len(valid_opposite)} hits, factor={opposite_factor:+.4f})"
    )

    return adjustment
