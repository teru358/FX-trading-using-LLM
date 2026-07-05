"""Multi-Timeframe (MTF) テクニカル分析のヘルパ。

各タイムフレーム (長期/中期/短期) で意味のある指標とパターンのセットを
定義し、user の global IndicatorToggleConfig / ChartPatternConfig と
AND を取って「このタイムフレームで計算すべきもの」を決定する。

設計原則:
  (user_enabled) AND (tf_relevant) = 実際に計算する

TF ごとの役割:
  long (90d daily)  — regime 判定 (大局レジーム): Ichimoku / SMA / ADX +
                      長期候補となるローソク足 + 形状パターン
  medium (14d 4h)   — structure 判定 (セットアップ): SMA / ADX +
                      全候補のローソク足 + 形状パターン
  short (2d 1h)     — timing 判定 (現状の単一 TF 挙動互換):
                      全指標 + 形状パターン以外 (48 本では検出困難)
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pandas as pd

from src.config import ChartPatternConfig, IndicatorToggleConfig
from src.data.indicators import IndicatorSummary, compute_indicators
from src.data.resample import interval_minutes, resample_ohlcv

if TYPE_CHECKING:
    from src.config import AnalysisConfig

logger = logging.getLogger(__name__)

TfSubset = Literal["regime", "structure", "full"]


# 指標: 各 subset で「強制 disable」にするフィールド名
_INDICATOR_DISABLE: dict[TfSubset, tuple[str, ...]] = {
    # regime (long 1d): RSI / MACD / BB / ATR を落として Ichimoku + SMA + ADX に絞る
    "regime":    ("rsi", "macd", "bollinger_bands", "atr"),
    # structure (medium 4h): 上記 + Ichimoku も落とす
    #   senkou span B (52 本) が 14 日 × 6 = 84 本でギリギリ、雲の精度が落ちる
    "structure": ("rsi", "macd", "bollinger_bands", "atr", "ichimoku"),
    # full (short 1h): 既存の単一 TF 挙動を維持 — 追加 disable なし
    "full":      (),
}


# パターン: 各 subset で「強制 disable」にするフィールド名
_PATTERN_DISABLE: dict[TfSubset, tuple[str, ...]] = {
    # regime (long 1d): doji / inside_bar / bb_squeeze / atr_contraction /
    #                   sr_breakout は長期 TF で頻出ノイズ or 短中期向け
    "regime":    ("doji", "inside_bar", "bb_squeeze", "atr_contraction", "sr_breakout"),
    # structure (medium 4h): ほぼ全パターン有効 (pattern 検出にちょうど良い本数)
    "structure": (),
    # full (short 1h): 形状パターン (double_top_bottom / head_shoulders /
    #                  triangle / range_bound) は 48 本では検出困難
    "full":      ("double_top_bottom", "head_shoulders", "triangle", "range_bound"),
}


def filter_indicator_cfg_for_tf(
    base: IndicatorToggleConfig,
    tf_subset: TfSubset,
) -> IndicatorToggleConfig:
    """base の IndicatorToggleConfig に tf_subset 固有の制限を重ねた新設定を返す。

    user の global 設定で既に False の項目はそのまま False。
    True でも tf_subset で強制 disable 対象なら False に落とす。
    """
    disables = _INDICATOR_DISABLE.get(tf_subset, ())
    if not disables:
        return base
    overrides = {name: False for name in disables}
    return replace(base, **overrides)


def filter_pattern_cfg_for_tf(
    base: ChartPatternConfig,
    tf_subset: TfSubset,
) -> ChartPatternConfig:
    """base の ChartPatternConfig に tf_subset 固有の制限を重ねた新設定を返す。"""
    disables = _PATTERN_DISABLE.get(tf_subset, ())
    if not disables:
        return base
    overrides = {name: False for name in disables}
    return replace(base, **overrides)


# ── multi-timeframe summary 計算 ──────────────────────────────


_TF_NAMES: tuple[str, ...] = ("long", "medium", "short")
_TF_TO_SUBSET: dict[str, TfSubset] = {
    "long": "regime",
    "medium": "structure",
    "short": "full",
}


def compute_mtf_summaries(
    df_1h: pd.DataFrame,
    analysis_cfg: "AnalysisConfig",
    timeframes: dict[str, dict],
    base_interval: str = "1h",
) -> dict[str, IndicatorSummary]:
    """基底足 OHLCV をベースに、各タイムフレームの IndicatorSummary を返す。

    Args:
        df_1h: 基底足 (base_interval で指定した粒度) の OHLCV (index=DatetimeIndex)。
               引数名は既存互換のため "df_1h" のままだが、実データは
               base_interval に応じて 15m/30m/1h などになりうる。
        analysis_cfg: 全体の analysis 設定 (indicator / pattern toggles)
        timeframes: {
            "long":   {"lookback_days": 90, "interval": "1d", "enabled": True},
            "medium": {"lookback_days": 14, "interval": "4h", "enabled": True},
            "short":  {"lookback_days": 2,  "interval": "1h", "enabled": True},
        }
        base_interval: df_1h の実際の基底足粒度。"15m" / "30m" / "1h" など。
                       デフォルトは "1h" (後方互換)。resample_ohlcv にそのまま
                       渡され、各 tf["interval"] が base_interval より細かい
                       場合は ValueError になる。

    Returns:
        {"long": IndicatorSummary, "medium": ..., "short": ...}
        disabled または十分な本数が取れなかった TF はキーが欠落する。

    データ不足時 (例: 90 日分の 1h データがない) はその TF を skip する。
    短期 TF は常に残し、長中期は best-effort。
    """
    summaries: dict[str, IndicatorSummary] = {}

    for tf_name in _TF_NAMES:
        tf = timeframes.get(tf_name)
        if not tf or not tf.get("enabled", True):
            continue

        interval = tf["interval"]
        lookback_days = tf["lookback_days"]

        # base より細かい TF は生成不能 — watch (base=1h) が day 用 mtf 設定
        # (short=15m) を読んだ場合など。スキップして他 TF は継続する。
        base_min = interval_minutes(base_interval)
        tf_min = interval_minutes(interval)
        if base_min is not None and tf_min is not None and tf_min < base_min:
            logger.info(
                "[MTF] skip %s (%s): finer than base %s", tf_name, interval, base_interval
            )
            continue

        # リサンプル (interval == base_interval は恒等)
        df_tf = resample_ohlcv(df_1h, interval, base_interval=base_interval)

        # lookback_days 分に切り詰める
        # interval から時間単位を概算
        bars_per_day = _bars_per_day_for_interval(interval)
        target_bars = lookback_days * bars_per_day
        if len(df_tf) > target_bars:
            df_tf = df_tf.iloc[-target_bars:]

        # 最低限の本数チェック (ichimoku 基準の 52 本を下回る場合でも
        # compute_indicators 内で fallback されるため slice はするがスキップはしない)
        if len(df_tf) < 20:
            # SMA 20 も計算できないほど少ないときのみスキップ
            continue

        subset = _TF_TO_SUBSET[tf_name]
        filtered_ind_cfg = filter_indicator_cfg_for_tf(
            analysis_cfg.indicators, subset,
        )
        filtered_pat_cfg = filter_pattern_cfg_for_tf(
            analysis_cfg.chart_patterns, subset,
        )

        _, summary = compute_indicators(
            df_tf,
            indicator_cfg=filtered_ind_cfg,
            pattern_cfg=filtered_pat_cfg,
        )
        summaries[tf_name] = summary

    return summaries


def _bars_per_day_for_interval(interval: str) -> int:
    """interval 文字列から 1 日あたりのおおよそのバー数を返す。

    分数は resample.py の interval_minutes を単一情報源とする (重複表の drift 防止)。
    未知 interval は従来どおり 24 に倒す。
    """
    minutes = interval_minutes(interval)
    if minutes is None:
        return 24  # fallback (未知 interval)
    return max(1, (24 * 60) // minutes)
