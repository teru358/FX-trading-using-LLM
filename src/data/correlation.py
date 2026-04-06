"""銘柄間の価格相関を計算するモジュール。

watch銘柄とtrade銘柄のClose系列からローリング相関係数を算出し、
LLMプロンプトに付加するための構造化データを返す。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from src.data.price_fetcher import PriceData

logger = logging.getLogger(__name__)

# trade対象に対して相関を計算するwatch銘柄のデフォルト
# (settings.yamlのinstrumentsから自動判別するが、フォールバック用)
_DEFAULT_ROLLING_WINDOW = 20


@dataclass
class PairCorrelation:
    """2銘柄間の相関情報。"""
    trade_symbol: str
    watch_symbol: str
    watch_display: str
    current_corr: float        # 直近ローリング相関
    prev_corr: float           # 1ウィンドウ前の相関
    change: float              # current - prev
    strength: str              # "strong" | "moderate" | "weak" | "none"
    direction: str             # "positive" | "negative" | "neutral"


def _classify_correlation(r: float) -> tuple[str, str]:
    """相関係数を強度・方向に分類する。"""
    abs_r = abs(r)
    if abs_r >= 0.7:
        strength = "strong"
    elif abs_r >= 0.4:
        strength = "moderate"
    elif abs_r >= 0.2:
        strength = "weak"
    else:
        strength = "none"

    if r > 0.1:
        direction = "positive"
    elif r < -0.1:
        direction = "negative"
    else:
        direction = "neutral"

    return strength, direction


def compute_correlations(
    trade_prices: dict[str, PriceData],
    watch_prices: dict[str, PriceData],
    watch_display_names: dict[str, str],
    rolling_window: int = _DEFAULT_ROLLING_WINDOW,
) -> list[PairCorrelation]:
    """trade銘柄×watch銘柄の相関係数を計算する。

    Args:
        trade_prices: {symbol: PriceData} trade対象のOHLCVデータ
        watch_prices: {symbol: PriceData} watch対象のOHLCVデータ
        watch_display_names: {symbol: display_name}
        rolling_window: ローリング相関のウィンドウサイズ（本数）

    Returns:
        PairCorrelation のリスト
    """
    results: list[PairCorrelation] = []

    for t_sym, t_data in trade_prices.items():
        t_close = t_data.df["Close"].dropna()
        if len(t_close) < rolling_window + 5:
            continue

        for w_sym, w_data in watch_prices.items():
            w_close = w_data.df["Close"].dropna()
            if len(w_close) < rolling_window + 5:
                continue

            # 共通インデックスで揃える
            combined = pd.DataFrame({
                "trade": t_close,
                "watch": w_close,
            }).dropna()

            if len(combined) < rolling_window + 5:
                continue

            # ローリング相関
            rolling_corr = combined["trade"].rolling(rolling_window).corr(combined["watch"])
            current = rolling_corr.iloc[-1]
            prev = rolling_corr.iloc[-(rolling_window + 1)] if len(rolling_corr) > rolling_window else current

            if pd.isna(current):
                continue

            current = float(current)
            prev = float(prev) if pd.notna(prev) else current
            change = current - prev
            strength, direction = _classify_correlation(current)

            results.append(PairCorrelation(
                trade_symbol=t_sym,
                watch_symbol=w_sym,
                watch_display=watch_display_names.get(w_sym, w_sym),
                current_corr=round(current, 3),
                prev_corr=round(prev, 3),
                change=round(change, 3),
                strength=strength,
                direction=direction,
            ))

    return results


def format_correlation_context(
    correlations: list[PairCorrelation],
    trade_symbol: str,
) -> str:
    """特定のtrade銘柄に関する相関データをプロンプト用テキストに整形する。"""
    relevant = [c for c in correlations if c.trade_symbol == trade_symbol]
    if not relevant:
        return ""

    lines = [f"=== Price Correlation ({_DEFAULT_ROLLING_WINDOW}-bar rolling) ==="]
    for c in relevant:
        # 変化が大きい場合にアラート
        alert = ""
        if abs(c.change) >= 0.2:
            alert = " ⚠ SHIFT" if c.change > 0 else " ⚠ DIVERGING"

        lines.append(
            f"{c.watch_display:16s} r={c.current_corr:+.3f} ({c.strength} {c.direction})"
            f"  prev={c.prev_corr:+.3f} Δ={c.change:+.3f}{alert}"
        )

    return "\n".join(lines)
