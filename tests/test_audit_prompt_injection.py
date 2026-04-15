"""audit_lessons の LLM プロンプト注入テスト。"""
from __future__ import annotations

from pathlib import Path

from src.analysis.price_analyzer import load_audit_lessons


def test_load_audit_lessons_empty(tmp_path: Path):
    """ファイルなしなら空文字。"""
    assert load_audit_lessons(tmp_path / "missing.md") == ""


def test_load_audit_lessons_with_content(tmp_path: Path):
    """ファイルに教訓があれば全文を返す (ヘッダー含む)。"""
    p = tmp_path / "audit_lessons.md"
    p.write_text(
        "# Audit Lessons\n\n"
        "## 2026-04-14 — USDJPY=X (s1)\n\n"
        "### [LLM-PROPOSED / USER-APPROVED] Test rule\n"
        "- 由来: ...\n",
        encoding="utf-8",
    )
    result = load_audit_lessons(p)
    assert "Test rule" in result
    assert "[LLM-PROPOSED / USER-APPROVED]" in result


def test_load_audit_lessons_header_only(tmp_path: Path):
    """ヘッダーのみで教訓ゼロなら空文字 (意味のある教訓がないため注入しない)。"""
    p = tmp_path / "audit_lessons.md"
    p.write_text(
        "# Audit Lessons\n\n承認済みルール...\n\n---\n\n",
        encoding="utf-8",
    )
    result = load_audit_lessons(p)
    assert result == ""
