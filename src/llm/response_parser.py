"""LLMレスポンスからJSON等を抽出する共通ユーティリティ。"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def _sanitize_json(raw: str) -> str:
    """LLM出力のJSON文字列をサニタイズする。"""
    # JS コメントの除去 (// ...)
    raw = re.sub(r"//[^\n]*", "", raw)
    # LLMが trailing comma を出力する場合の補正: ",}" → "}", ",]" → "]"
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # NaN / Infinity / -Infinity / undefined / None → null
    raw = re.sub(r"\bNaN\b", "null", raw)
    raw = re.sub(r"\b-?Infinity\b", "null", raw)
    raw = re.sub(r"\bundefined\b", "null", raw)
    raw = re.sub(r"\bNone\b", "null", raw)
    # "N/A" / "n/a" / "-" を値として使っている場合
    raw = re.sub(r':\s*"[Nn]/?[Aa]"', ": null", raw)
    raw = re.sub(r':\s*"-"', ": null", raw)
    # 数値の先頭 +記号を除去 (JSON非準拠: +0.16 → 0.16)
    raw = re.sub(r":\s*\+(\d)", r": \1", raw)
    raw = re.sub(r"\[\s*\+(\d)", r"[\1", raw)
    # 値なし ("key": ,) / ("key": }) / ("key": ])
    raw = re.sub(r":\s*,", ": null,", raw)
    raw = re.sub(r":\s*}", ": null}", raw)
    raw = re.sub(r":\s*\]", ": null]", raw)
    # 単一引用符→二重引用符
    raw = re.sub(r"(?<=[\[,{:\s])'([^']*)'(?=[,\]}:\s])", r'"\1"', raw)
    # 引用符なしの文字列値（数値・true/false/null以外） "key": long → "key": "long"
    raw = re.sub(
        r'("[\w_]+")\s*:\s*([a-zA-Z_][\w_]*)\s*([,}\]])',
        lambda m: (
            f'{m.group(1)}: {m.group(2)}{m.group(3)}'
            if m.group(2) in ("true", "false", "null")
            else f'{m.group(1)}: "{m.group(2)}"{m.group(3)}'
        ),
        raw,
    )
    return raw


def extract_json(text: str) -> dict:
    """LLM応答テキストから最後のJSONオブジェクトを抽出してパースする。

    deepseek-r1 等の <think>...</think> タグを自動除去してからパースを試みる。
    ネストされたJSONオブジェクト（atr_params_suggestion 等）にも対応するため、
    最も外側の {} マッチを優先し、パース失敗時のみ浅いマッチにフォールバックする。
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # 深いネスト (最も外側の {…}) を先に試行
    deep_matches = list(re.finditer(r"\{.*\}", text, re.DOTALL))
    if deep_matches:
        raw = _sanitize_json(deep_matches[-1].group())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    # フォールバック: 浅いネスト (ネストなし {…}) の最後をパース
    shallow_matches = list(re.finditer(r"\{[^{}]+\}", text, re.DOTALL))
    if not shallow_matches:
        raise ValueError(f"No JSON block found in LLM response. Response: {text[:500]}")
    raw = _sanitize_json(shallow_matches[-1].group())
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[PARSER] JSON parse failed after sanitize: {e}\nRaw: {raw[:300]}")
        raise
