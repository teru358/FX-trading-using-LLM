# tests/test_user_notes_plan_section.py
from src.analysis.price_analyzer import load_user_notes


def test_load_plan_section(tmp_path):
    p = tmp_path / "user_notes.md"
    p.write_text(
        "## price\nprice note\n\n## plan\nprefer trend-following\n\n## news\nnews note\n",
        encoding="utf-8",
    )
    assert load_user_notes(p, "plan") == "prefer trend-following"
    assert load_user_notes(p, "news") == "news note"


def test_plan_section_absent_returns_empty(tmp_path):
    p = tmp_path / "user_notes.md"
    p.write_text("## news\nonly news\n", encoding="utf-8")
    assert load_user_notes(p, "plan") == ""


def test_read_failure_returns_empty_with_warning(tmp_path, caplog):
    # fail-soft (spec §2.D): 読込失敗は warning + 空文字
    p = tmp_path / "user_notes.md"
    p.write_bytes(b"\xff\xfe\x00 broken \x9c")  # UnicodeDecodeError を誘発
    import logging
    with caplog.at_level(logging.WARNING):
        assert load_user_notes(p, "plan") == ""
    assert any("user_notes" in r.message for r in caplog.records)
