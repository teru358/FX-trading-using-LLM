"""Audit review の対話 UX。

入力パーサ:
  1,2,3     → accept
  a         → accept_all
  r         → regenerate (hint なし)
  r <hint>  → regenerate with hint
  1,2+r     → partial (指定を accept + 残りを regenerate)
  q         → skip (このトレードから次へ)
  s         → stop (review 全体を中断)
  (空)      → skip
  (その他)  → error
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from src.analysis.audit_post_hoc import LessonCandidate

logger = logging.getLogger(__name__)

ReviewKind = Literal[
    "accept",
    "accept_all",
    "regenerate",
    "partial",
    "skip_trade",
    "stop_review",
    "error",
]


@dataclass
class ReviewAction:
    kind: ReviewKind
    indices: list[int] = field(default_factory=list)
    hint: str = ""


def parse_review_input(line: str) -> ReviewAction:
    """ユーザー入力を ReviewAction にパースする。"""
    line = line.strip()
    if not line:
        return ReviewAction(kind="skip_trade")
    if line == "a":
        return ReviewAction(kind="accept_all")
    if line == "q":
        return ReviewAction(kind="skip_trade")
    if line == "s":
        return ReviewAction(kind="stop_review")

    if line == "r":
        return ReviewAction(kind="regenerate")
    if line.startswith("r "):
        return ReviewAction(kind="regenerate", hint=line[2:].strip())

    if "+r" in line:
        nums_part = line.split("+r")[0]
        try:
            indices = [int(x.strip()) for x in nums_part.split(",") if x.strip()]
            return ReviewAction(kind="partial", indices=indices)
        except ValueError:
            return ReviewAction(kind="error")

    try:
        indices = [int(x.strip()) for x in line.split(",") if x.strip()]
        if not indices:
            return ReviewAction(kind="error")
        return ReviewAction(kind="accept", indices=indices)
    except ValueError:
        return ReviewAction(kind="error")


def append_lessons_to_file(
    path: Path,
    session_info: dict,
    lessons: list[LessonCandidate],
) -> None:
    """承認された教訓を audit_lessons.md に追記する。

    ファイルがなければヘッダー付きで新規作成。
    各教訓は `[LLM-PROPOSED / USER-APPROVED]` タグ + 出処の trade 情報を付与。
    """
    if not lessons:
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        header = (
            "# Audit Lessons\n\n"
            "LLM が生成しユーザーが承認した取引改善ルール。\n"
            "`src/analysis/price_analyzer.py` の system prompt に注入される。\n\n"
            "---\n\n"
        )
        path.write_text(header, encoding="utf-8")

    closed_at = session_info.get("closed_at") or datetime.now()
    date_str = closed_at.strftime("%Y-%m-%d")
    pair = session_info.get("pair", "?")
    pnl = session_info.get("realized_pnl", 0)
    flag = session_info.get("flag", "?")
    sid = session_info.get("session_id", "?")

    block_lines = [f"## {date_str} — {pair} ({sid})", ""]
    for lesson in lessons:
        block_lines.append(f"### [LLM-PROPOSED / USER-APPROVED] {lesson.rule_text}")
        block_lines.append(f"- 由来: {date_str} {pair} (pnl={pnl:+,.0f}, flag={flag})")
        block_lines.append(f"- 根拠: {lesson.rationale}")
        block_lines.append(f"- 適用条件: {lesson.applicability}")
        if lesson.hint_used:
            block_lines.append(f"- 再生成ヒント: {lesson.hint_used}")
        block_lines.append(f"- 承認日: {datetime.now().strftime('%Y-%m-%d')}")
        block_lines.append("")
    block_lines.append("---")
    block_lines.append("")

    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block_lines))
    logger.info(f"[AUDIT] Appended {len(lessons)} lessons to {path}")


_MAX_REGEN_RETRIES = 2  # 初回 + 2 回 = 合計 3 attempts


async def interactive_review(
    items: list[dict],
    lessons_path: Path,
    input_reader,
    regen_fn,
) -> int:
    """対話 review ループ。

    items: 各要素は dict で以下のキーを持つ
        session, post_hoc, counterfactuals, vol_percentile, candidates, flag
    lessons_path: audit_lessons.md の path
    input_reader: stdin を abstract した callable (テストで mock 可能)
    regen_fn: LLM 再生成を行う async callable (session, hint='') -> list[LessonCandidate]
    Returns: 採用された教訓の合計数
    """
    total_accepted = 0

    for idx, item in enumerate(items, start=1):
        session = item["session"]
        candidates: list[LessonCandidate] = list(item.get("candidates") or [])
        retries_left = _MAX_REGEN_RETRIES
        stop_review = False

        while True:
            if not candidates:
                print(f"[{idx}/{len(items)}] {session.pair} {session.direction.upper()} "
                      f"conf={session.signal_confidence:.2f} pnl={session.realized_pnl:+.0f}")
                print("  (候補なし、次へ)")
                break

            print(f"\n[{idx}/{len(items)}] {session.pair} {session.direction.upper()} "
                  f"conf={session.signal_confidence:.2f} pnl={session.realized_pnl:+.0f}")
            print(f"flag={item['flag']}")
            print("LLM が生成した改善候補:")
            for i, c in enumerate(candidates, start=1):
                print(f"  [{i}] {c.rule_text}")
                print(f"      根拠: {c.rationale}")
                print(f"      適用条件: {c.applicability}")
            print("\n操作: 1,2 / a / r / r <hint> / 1,2+r / q / s")
            print(f"  (retries left: {retries_left})")

            line = input_reader("> ")
            action = parse_review_input(line)

            if action.kind == "error":
                print("  無効な入力です、再度入力してください")
                continue

            if action.kind == "stop_review":
                stop_review = True
                break

            if action.kind == "skip_trade":
                break

            if action.kind == "accept":
                selected = [candidates[i - 1] for i in action.indices
                            if 1 <= i <= len(candidates)]
                append_lessons_to_file(
                    path=lessons_path,
                    session_info={
                        "session_id": session.session_id,
                        "pair": session.pair,
                        "realized_pnl": session.realized_pnl,
                        "flag": item["flag"],
                        "closed_at": session.closed_at,
                    },
                    lessons=selected,
                )
                total_accepted += len(selected)
                break

            if action.kind == "accept_all":
                append_lessons_to_file(
                    path=lessons_path,
                    session_info={
                        "session_id": session.session_id,
                        "pair": session.pair,
                        "realized_pnl": session.realized_pnl,
                        "flag": item["flag"],
                        "closed_at": session.closed_at,
                    },
                    lessons=candidates,
                )
                total_accepted += len(candidates)
                break

            if action.kind == "regenerate":
                if retries_left <= 0:
                    print("  リトライ上限に達しました (再生成は使えません)")
                    continue
                retries_left -= 1
                new_cands = await regen_fn(session, hint=action.hint)
                if not new_cands:
                    print("  再生成に失敗しました、現候補から選ぶかスキップしてください")
                    continue
                candidates = list(new_cands)
                continue

            if action.kind == "partial":
                kept = [candidates[i - 1] for i in action.indices
                        if 1 <= i <= len(candidates)]
                append_lessons_to_file(
                    path=lessons_path,
                    session_info={
                        "session_id": session.session_id,
                        "pair": session.pair,
                        "realized_pnl": session.realized_pnl,
                        "flag": item["flag"],
                        "closed_at": session.closed_at,
                    },
                    lessons=kept,
                )
                total_accepted += len(kept)
                if retries_left <= 0:
                    print("  リトライ上限に達したため再生成はスキップ、次へ")
                    break
                retries_left -= 1
                new_cands = await regen_fn(session, hint="")
                if not new_cands:
                    print("  追加候補なし、次へ")
                    break
                candidates = list(new_cands)
                continue

        if stop_review:
            print("review を中断します")
            break

    return total_accepted
