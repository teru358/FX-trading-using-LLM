"""後方互換 shim — 実体は ``src/cycles/`` パッケージに移動した。

歴史的に多くのモジュール (main.py / cli.py / tui.py / api/server.py / views.py /
tests) が ``from src.trading_cycle import ...`` で
公開関数・私有ヘルパーの両方を import している。一括置換する代わりに、
このファイルが ``src/cycles/`` から全シンボルを re-export することで既存呼び出しを
無傷のまま動かす。

新規コードは ``src/cycles/`` 直下を直接 import すること。
"""
from __future__ import annotations

# 公開 API: サイクル本体 + 同期ラッパー
from src.cycles.exit_check import exit_check_cycle, run_exit_check_cycle
from src.cycles.trading import (
    _adjust_signal_with_rag,
    _apply_atr_sltp_to_signal,
    _build_trading_runtime,
    _execute_one_signal,
    _finalize_closed_orders,
    _phase_analyze_pairs,
    _phase_close_sl_tp,
    _phase_execute_signals,
    _phase_review_open_positions,
    _process_pair,
    run_trading_cycle,
    trading_cycle,
)

# 共通ヘルパー: tests / views.py / api/server.py から参照される
from src.cycles._helpers import (
    _build_macro_context,
    _compute_atr_from_price_data,
    _fetch_and_compute_atr,
    _get_ohlcv,
    _get_price,
    _summarize_pair,
)

__all__ = [
    # サイクル
    "trading_cycle",
    "run_trading_cycle",
    "exit_check_cycle",
    "run_exit_check_cycle",
    # 共通ヘルパー
    "_get_price",
    "_get_ohlcv",
    "_compute_atr_from_price_data",
    "_fetch_and_compute_atr",
    "_build_macro_context",
    "_summarize_pair",
    # trading.py 内部ヘルパー (テスト / 内部参照のため公開維持)
    "_process_pair",
    "_apply_atr_sltp_to_signal",
    "_finalize_closed_orders",
    "_phase_close_sl_tp",
    "_phase_analyze_pairs",
    "_phase_review_open_positions",
    "_adjust_signal_with_rag",
    "_execute_one_signal",
    "_phase_execute_signals",
    "_build_trading_runtime",
]
