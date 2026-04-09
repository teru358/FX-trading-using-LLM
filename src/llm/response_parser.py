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
    raw = matches[-1].group()
    # JS コメントの除去 (// ...)
    raw = re.sub(r"//[^\n]*", "", raw)
    # LLMが trailing comma を出力する場合の補正: ",}" → "}", ",]" → "]"
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # LLMが NaN / Infinity / -Infinity / undefined / N/A を出力する場合の補正
    raw = re.sub(r"\bNaN\b", "null", raw)
    raw = re.sub(r"\b-?Infinity\b", "null", raw)
    raw = re.sub(r"\bundefined\b", "null", raw)
    raw = re.sub(r':\s*"?N/?A"?', ": null", raw)
    raw = re.sub(r':\s*"-"', ": null", raw)
    # LLMが値なし ("key": ,) / ("key": }) を出力する場合の補正
    raw = re.sub(r":\s*,", ": null,", raw)
    raw = re.sub(r":\s*}", ": null}", raw)
    # 単一引用符→二重引用符 (JSON非準拠の 'value' を修正)
    raw = re.sub(r"(?<=[\[,{:\s])'([^']*)'(?=[,\]}:\s])", r'"\1"', raw)
    return json.loads(raw)
