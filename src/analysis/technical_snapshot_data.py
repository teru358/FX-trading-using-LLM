# src/analysis/technical_snapshot_data.py
"""決定的 technical snapshot の入力 DTO。

analyze_price_action (LLM) 廃止後、collector は tech_score / mtf_score /
chart_patterns から直接この dataclass を組んで add_snapshot に渡す。
PriceAnalysis (旧 cycle 用 signal DTO) とは別物。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TechnicalSnapshotData:
    pair: str
    analyzed_at: datetime
    bias_score: float
    confidence: float
    direction_bias: str  # "long" | "short" | "neutral"
    # MTF (単一 TF fallback 時は alignment=None / tf_scores={})
    mtf_alignment: float | None = None
    tf_scores: dict[str, dict] = field(default_factory=dict)   # {"long": {"score":..,"direction":..}}
    components: dict[str, float] = field(default_factory=dict)  # {"sma":.., "adx_factor":..}
    patterns: list[str] = field(default_factory=list)
