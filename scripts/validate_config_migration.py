"""config 移行の preflight 検証。

移行前スナップショット (dump_config_snapshot.py の出力) と、
新コードで読んだ新 config を比較する。想定外の差分があれば非 0 で終了する。

使い方:
    # 1. 移行前の revision で:
    uv run python scripts/dump_config_snapshot.py /tmp/before.json
    # 2. 移行後のコード + 新 config で:
    uv run python scripts/validate_config_migration.py /tmp/before.json <新configディレクトリ>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# scripts/ 自身も import path に追加する (scripts/ は package ではないため)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config
from dump_config_snapshot import flatten

# 移行で消えるキー (旧スナップショットにのみ存在してよい)
EXPECTED_REMOVED = {
    "schedule.timezone",
    "news_collection.timezone",
    "economic_calendar.fetch_timezone",
    "rag.embedding_provider",
    "rag.embedding_model",
    "rag.embedding_base_url",
}

# 移行で増えるキー (新 config にのみ存在してよい)
EXPECTED_ADDED = {
    "timezone",
    "embedding.provider",
    "embedding.model",
    "embedding.base_url",
}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    before_path = Path(sys.argv[1])
    new_dir = Path(sys.argv[2])

    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = flatten(load_config(new_dir / "settings.yaml"))

    removed = set(before) - set(after)
    added = set(after) - set(before)
    changed = {k for k in set(before) & set(after) if before[k] != after[k]}

    unexpected_removed = sorted(removed - EXPECTED_REMOVED)
    unexpected_added = sorted(added - EXPECTED_ADDED)
    unexpected_changed = sorted(changed)

    if removed & EXPECTED_REMOVED:
        print("=== expected removals (OK) ===")
        for k in sorted(removed & EXPECTED_REMOVED):
            print(f"  - {k} = {before[k]}")

    if added & EXPECTED_ADDED:
        print("\n=== expected additions (OK) ===")
        for k in sorted(added & EXPECTED_ADDED):
            print(f"  + {k} = {after[k]}")

    problems = 0

    if unexpected_removed:
        print("\n=== UNEXPECTED removals ===")
        for k in unexpected_removed:
            print(f"  - {k} = {before[k]}")
        problems += len(unexpected_removed)

    if unexpected_added:
        print("\n=== UNEXPECTED additions ===")
        for k in unexpected_added:
            print(f"  + {k} = {after[k]}")
        problems += len(unexpected_added)

    if unexpected_changed:
        print("\n=== UNEXPECTED value changes ===")
        for k in unexpected_changed:
            print(f"  ~ {k}: {before[k]} -> {after[k]}")
        problems += len(unexpected_changed)

    if problems:
        print(
            f"\nFAIL: {problems} unexpected difference(s). "
            "This usually means a block was dropped or misspelled during the split, "
            "and its values fell back to schema defaults."
        )
        return 1

    print("\nOK: only the expected migration differences were found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
