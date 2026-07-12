"""src.utils.json_safe.load_json_column の単体テスト。

JSON 列を安全に読む共有ユーティリティ。NULL/空/破損 JSON/型不一致は
default に倒す (context_builder と API route が共有する)。
"""
from __future__ import annotations

from src.utils.json_safe import load_json_column


def test_valid_json_list():
    assert load_json_column('["a", "b"]', []) == ["a", "b"]


def test_valid_json_dict():
    assert load_json_column('{"k": 1}', {}) == {"k": 1}


def test_none_returns_default():
    assert load_json_column(None, []) == []
    assert load_json_column(None, {}) == {}


def test_empty_string_returns_default():
    assert load_json_column("", []) == []
    assert load_json_column("", {}) == {}


def test_malformed_json_returns_default():
    assert load_json_column("{bad", []) == []
    assert load_json_column("{not valid", {}) == {}


def test_type_mismatch_object_when_default_list():
    # JSON object だが default が [] → [] に倒す
    assert load_json_column('{"k": 1}', []) == []


def test_type_mismatch_list_when_default_dict():
    # JSON list だが default が {} → {} に倒す
    assert load_json_column('["a", "b"]', {}) == {}


def test_default_object_identity_preserved():
    # default をそのまま返す (同一オブジェクト) — 破損時
    default: list = []
    assert load_json_column("{bad", default) is default
