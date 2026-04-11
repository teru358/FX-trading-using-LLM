"""プロンプトサイズ推定ユーティリティ。

起動時に設定に基づいてプロンプトの固定部分のサイズを計測し、
LLMへ送るトークン量の概算を提供する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import AppConfig


# config キー → 実際に chart_patterns リストに入る代表パターン名
_CONFIG_KEY_TO_PATTERN: dict[str, str] = {
    "hammer":               "hammer",
    "shooting_star":        "shooting_star",
    "engulfing":            "bullish_engulfing",
    "doji":                 "doji",
    "morning_evening_star": "morning_star",
    "three_soldiers_crows": "three_white_soldiers",
    "pin_bar":              "bullish_pin_bar",
    "inside_bar":           "inside_bar",
    "double_top_bottom":    "double_bottom",
    "head_shoulders":       "head_shoulders",
    "triangle":             "ascending_triangle",
    "range_bound":          "range_bound",
    "bb_squeeze":           "bb_squeeze",
    "atr_contraction":      "atr_contraction",
    "sr_breakout":          "sr_breakout_bullish",
}


@dataclass
class PromptSizeEstimate:
    system_chars: int
    template_overhead_chars: int
    classic_chars: int
    ichimoku_chars: int   # 0 if disabled
    pattern_chars: int    # 0 if no patterns enabled
    active_patterns: int

    @property
    def indicator_total(self) -> int:
        return self.classic_chars + self.ichimoku_chars + self.pattern_chars

    @property
    def fixed_total(self) -> int:
        return self.system_chars + self.template_overhead_chars + self.indicator_total

    @property
    def fixed_tokens_est(self) -> int:
        """文字数 // 4 による推定トークン数（英語基準、±15%程度の誤差あり）。"""
        return self.fixed_total // 4


def estimate_prompt_size(config: "AppConfig") -> PromptSizeEstimate:
    """設定に基づいてプロンプト固定部分のサイズを推定する。

    ダミーデータで format_for_llm() を実際に呼び出してセクション別に計測する。
    可変部分（ニュース / RAG / リフレクション）は含まない。
    """
    import pandas as pd

    from src.analysis.prompt_loader import render_prompt, load_prompt
    from src.data.indicator_formatter import format_for_llm
    from src.data.indicators import IndicatorSummary
    from src.data.price_fetcher import PriceData

    dummy_price = PriceData(
        symbol="DUMMY",
        df=pd.DataFrame(),
        current_price=150.000,
        fetched_at=datetime.now(),
    )

    def _make_summary(ichimoku: bool, patterns: list[str]) -> IndicatorSummary:
        return IndicatorSummary(
            sma_20=150.123, sma_50=149.876, sma_200=148.543,
            ema_12=150.234, ema_26=150.012,
            rsi_14=55.3,
            macd_line=0.00123, macd_signal=0.00098, macd_histogram=0.00025,
            bb_upper=151.234, bb_lower=148.765, bb_pct_b=0.654,
            atr_14=0.543,
            adx_14=28.7,
            current_price=150.000,
            tenkan_sen=150.123 if ichimoku else 0.0,
            kijun_sen=149.876 if ichimoku else 0.0,
            senkou_span_a=150.500 if ichimoku else 0.0,
            senkou_span_b=149.250 if ichimoku else 0.0,
            chikou_span=150.100 if ichimoku else 0.0,
            kumo_top=150.500 if ichimoku else 0.0,
            kumo_bottom=149.250 if ichimoku else 0.0,
            price_vs_kumo="above" if ichimoku else "unknown",
            tk_cross="bullish" if ichimoku else "none",
            kumo_color="green" if ichimoku else "unknown",
            ichimoku_signal="bullish" if ichimoku else "neutral",
            recent_highs=[151.234, 150.876],
            recent_lows=[148.765, 149.123],
            trend_direction="uptrend",
            chart_patterns=patterns,
            pattern_bias="bullish" if patterns else "neutral",
        )

    # ── classic（ichimoku/patterns なし）──────────────────────────────
    classic_text = format_for_llm(dummy_price, _make_summary(ichimoku=False, patterns=[]))
    classic_chars = len(classic_text)

    # ── ichimoku セクション ──────────────────────────────────────────
    ichi_chars = 0
    if config.analysis.indicators.ichimoku:
        with_ichi = format_for_llm(dummy_price, _make_summary(ichimoku=True, patterns=[]))
        ichi_chars = len(with_ichi) - classic_chars

    # ── chart patterns セクション ────────────────────────────────────
    cp = config.analysis.chart_patterns
    active_pattern_names = [
        _CONFIG_KEY_TO_PATTERN[key]
        for key, enabled in [
            ("hammer",               cp.hammer),
            ("shooting_star",        cp.shooting_star),
            ("engulfing",            cp.engulfing),
            ("doji",                 cp.doji),
            ("morning_evening_star", cp.morning_evening_star),
            ("three_soldiers_crows", cp.three_soldiers_crows),
            ("pin_bar",              cp.pin_bar),
            ("inside_bar",           cp.inside_bar),
            ("double_top_bottom",    cp.double_top_bottom),
            ("head_shoulders",       cp.head_shoulders),
            ("triangle",             cp.triangle),
            ("range_bound",          cp.range_bound),
            ("bb_squeeze",           cp.bb_squeeze),
            ("atr_contraction",      cp.atr_contraction),
            ("sr_breakout",          cp.sr_breakout),
        ]
        if enabled
    ]
    pattern_chars = 0
    if active_pattern_names:
        with_patterns = format_for_llm(
            dummy_price, _make_summary(ichimoku=False, patterns=active_pattern_names)
        )
        pattern_chars = len(with_patterns) - classic_chars

    # ── price_user.j2 骨格（可変部は空文字で埋める）────────────
    template_overhead = len(render_prompt(
        "price_user.j2",
        formatted_data="", pair="",
        news_context="", reflection_context="",
        previous_analysis="", macro_context="", user_context="",
        tech_score_context="", mtf_score_context="",
    ))

    return PromptSizeEstimate(
        system_chars=len(load_prompt("price_system.txt")),
        template_overhead_chars=template_overhead,
        classic_chars=classic_chars,
        ichimoku_chars=ichi_chars,
        pattern_chars=pattern_chars,
        active_patterns=len(active_pattern_names),
    )
