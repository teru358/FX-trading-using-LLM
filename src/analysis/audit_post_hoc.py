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
