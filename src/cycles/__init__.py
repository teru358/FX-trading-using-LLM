"""トレーディングサイクルパッケージ。

3つの独立サイクルを物理分離して保守性を高める:

- ``trading``      — フル取引サイクル (LLM 使用、新規発注あり)
- ``exit_check``   — 出口専用軽量サイクル (LLM 不使用、SL/TP & ポジション再評価のみ)
- ``forecast``     — 予測検証サイクル (LLM 不使用、将来検証用シグナル蓄積)

3サイクル共通のヘルパーは ``_helpers`` モジュールに集約する。
後方互換のため ``src/trading_cycle.py`` が同じ名前で全部を re-export している。
"""
from src.cycles.exit_check import exit_check_cycle, run_exit_check_cycle
from src.cycles.forecast import forecast_cycle, run_forecast_cycle
from src.cycles.trading import run_trading_cycle, trading_cycle

__all__ = [
    "trading_cycle",
    "run_trading_cycle",
    "exit_check_cycle",
    "run_exit_check_cycle",
    "forecast_cycle",
    "run_forecast_cycle",
]
