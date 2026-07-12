"""銘柄間の価格相関を計算するモジュール。

watch銘柄とtrade銘柄のClose系列からローリング相関係数を算出し、
LLMプロンプトに付加するための構造化データを返す。

NOTE: すべての公開関数（compute_correlations, format_correlation_context等）は
src/ 内に呼び出し元が存在しないため削除済み（旧collectorの相関ブロック削除に伴う）。
"""

from __future__ import annotations
