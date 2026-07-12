"""JSON 列を安全に読む共有ユーティリティ。

DB の JSON 文字列列 (patterns_json / tf_scores_json / components_json など) は
手動デバッグや部分書き込みで破損しうる。1 行の破損で読み取り側全体を落とさない
よう、NULL/空/破損 JSON/型不一致はすべて呼び出し側が渡した default に倒す。

context_builder (planner context) と API route (/tech 診断) が同じ意味論で
共有するため、依存の軽いこのモジュールに切り出している。
"""
from __future__ import annotations

import json
from typing import Any


def load_json_column(raw: Any, default: Any) -> Any:
    """JSON 列を安全に読む。NULL/空/破損 JSON/型不一致は default に倒す。

    Args:
        raw: DB から読んだ生の JSON 文字列 (None / 空文字 / 破損もありうる)。
        default: フォールバック値。返り値の型判定にも使う (``type(default)``)。

    Returns:
        パース結果が ``type(default)`` と一致すればその値、そうでなければ
        ``default`` そのもの (同一オブジェクト)。
    """
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default
