"""audit_reviewer のテスト。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from src.analysis.audit_post_hoc import LessonCandidate
from src.analysis.audit_reviewer import (
    ReviewAction,
    append_lessons_to_file,
    interactive_review,
    parse_review_input,
)
from tests.fixtures.audit import make_fake_session


# --- Task 17: parser tests ---


def test_parse_accept_single():
    a = parse_review_input("1")
    assert a.kind == "accept"
    assert a.indices == [1]


def test_parse_accept_multiple():
    a = parse_review_input("1,2,3")
    assert a.kind == "accept"
    assert a.indices == [1, 2, 3]


def test_parse_accept_all():
    a = parse_review_input("a")
    assert a.kind == "accept_all"


def test_parse_regenerate_plain():
    a = parse_review_input("r")
    assert a.kind == "regenerate"
    assert a.hint == ""


def test_parse_regenerate_with_hint():
    a = parse_review_input("r focus on CPI timing")
    assert a.kind == "regenerate"
    assert a.hint == "focus on CPI timing"


def test_parse_partial_regenerate():
    a = parse_review_input("1,2+r")
    assert a.kind == "partial"
    assert a.indices == [1, 2]


def test_parse_quit():
    assert parse_review_input("q").kind == "skip_trade"


def test_parse_stop():
    assert parse_review_input("s").kind == "stop_review"


def test_parse_empty_is_skip():
    assert parse_review_input("").kind == "skip_trade"


def test_parse_invalid_returns_error():
    a = parse_review_input("xyz")
    assert a.kind == "error"


# --- Task 18: lessons file appender tests ---


def _mk_cand(text: str) -> LessonCandidate:
    return LessonCandidate(
        rule_text=text,
        rationale="test rationale",
        applicability="all pairs",
        generated_at=datetime(2026, 4, 14, 12, 0),
    )


def test_append_lessons_creates_file(tmp_path: Path):
    """ファイルがない場合は新規作成。"""
    p = tmp_path / "audit_lessons.md"
    append_lessons_to_file(
        path=p,
        session_info={"session_id": "s1", "pair": "USDJPY=X",
                      "realized_pnl": -2000, "flag": "CONF_MISS",
                      "closed_at": datetime(2026, 4, 14, 12, 0)},
        lessons=[_mk_cand("Rule A")],
    )
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "# Audit Lessons" in content
    assert "Rule A" in content
    assert "[LLM-PROPOSED / USER-APPROVED]" in content


def test_append_lessons_appends(tmp_path: Path):
    """既存ファイルには追記する。"""
    p = tmp_path / "audit_lessons.md"
    p.write_text("# Audit Lessons\n\n", encoding="utf-8")
    append_lessons_to_file(
        path=p,
        session_info={"session_id": "s1", "pair": "USDJPY=X",
                      "realized_pnl": -2000, "flag": "CONF_MISS",
                      "closed_at": datetime(2026, 4, 14, 12, 0)},
        lessons=[_mk_cand("Rule A")],
    )
    append_lessons_to_file(
        path=p,
        session_info={"session_id": "s2", "pair": "EURUSD=X",
                      "realized_pnl": -1500, "flag": "CONF_MISS",
                      "closed_at": datetime(2026, 4, 14, 13, 0)},
        lessons=[_mk_cand("Rule B")],
    )
    content = p.read_text(encoding="utf-8")
    assert "Rule A" in content
    assert "Rule B" in content
    assert content.count("# Audit Lessons") == 1  # ヘッダーは 1 回のみ


# --- Task 19: interactive review tests ---


def _inputs(lines: list[str]):
    """input stream を iter で返すヘルパー。"""
    it = iter(lines)

    def _reader(prompt: str = "") -> str:
        return next(it)

    return _reader


def test_interactive_review_accept_first_trade(tmp_path: Path):
    """1 トレード目の候補 1 番を accept → lessons に追記。"""
    s = make_fake_session("r1", pnl=-2000, conf=0.82)
    review_item = {
        "session": s,
        "post_hoc": None,
        "counterfactuals": None,
        "vol_percentile": None,
        "candidates": [_mk_cand("First rule"), _mk_cand("Second rule")],
        "flag": "CONF_MISS",
    }

    lessons_path = tmp_path / "audit_lessons.md"

    async def fake_regen(*args, **kwargs):
        return []

    asyncio.run(interactive_review(
        items=[review_item],
        lessons_path=lessons_path,
        input_reader=_inputs(["1"]),
        regen_fn=fake_regen,
    ))
    assert lessons_path.exists()
    content = lessons_path.read_text(encoding="utf-8")
    assert "First rule" in content
    assert "Second rule" not in content


def test_interactive_review_regenerate(tmp_path: Path):
    """再生成 → 新候補から accept。"""
    s = make_fake_session("r1", pnl=-2000)
    initial = [_mk_cand("Weak rule")]
    regenerated = [_mk_cand("Better rule")]

    regen_calls = {"count": 0}

    async def fake_regen(session, hint=""):
        regen_calls["count"] += 1
        return regenerated

    review_item = {
        "session": s, "post_hoc": None, "counterfactuals": None,
        "vol_percentile": None, "candidates": initial, "flag": "CONF_MISS",
    }
    lessons_path = tmp_path / "audit_lessons.md"
    asyncio.run(interactive_review(
        items=[review_item],
        lessons_path=lessons_path,
        input_reader=_inputs(["r focus on X", "1"]),
        regen_fn=fake_regen,
    ))
    assert regen_calls["count"] == 1
    content = lessons_path.read_text(encoding="utf-8")
    assert "Better rule" in content
    assert "Weak rule" not in content


def test_interactive_review_stop_mid_way(tmp_path: Path):
    """2 トレード目で s (stop_review) → 後続は処理されない。"""
    s1 = make_fake_session("r1", pnl=-2000)
    s2 = make_fake_session("r2", pnl=-1500)
    items = [
        {"session": s1, "post_hoc": None, "counterfactuals": None,
         "vol_percentile": None, "candidates": [_mk_cand("A")], "flag": "CONF_MISS"},
        {"session": s2, "post_hoc": None, "counterfactuals": None,
         "vol_percentile": None, "candidates": [_mk_cand("B")], "flag": "CONF_MISS"},
    ]
    lessons_path = tmp_path / "audit_lessons.md"

    async def fake_regen(*args, **kwargs):
        return []

    asyncio.run(interactive_review(
        items=items,
        lessons_path=lessons_path,
        input_reader=_inputs(["1", "s"]),
        regen_fn=fake_regen,
    ))
    content = lessons_path.read_text(encoding="utf-8")
    assert "A" in content
    assert "B" not in content
