"""LLMレスポンスからJSON等を抽出する共通ユーティリティ。"""

from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict:
    """LLM応答テキストから最後のJSONオブジェクトを抽出してパースする。

    deepseek-r1 等の <think>...</think> タグを自動除去してからパースを試みる。
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # 浅いネストの {} を先に試行（確実性が高い）
    matches = list(re.finditer(r"\{[^{}]+\}", text, re.DOTALL))
    if not matches:
        # 深いネストも許容
        matches = list(re.finditer(r"\{.*\}", text, re.DOTALL))
    if not matches:
        raise ValueError(f"No JSON block found in LLM response. Response: {text[:500]}")
    return json.loads(matches[-1].group())
