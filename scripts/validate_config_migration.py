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

# 移設の対応表: 新キー ← 移設元の旧キー群。
# 単に「消えてよい / 増えてよい」を許可するだけでは、移設の過程で値が化けても
# 検出できない (旧キーは removed、新キーは added として別々に許可されるため)。
# 値の一致まで検証する。
MIGRATIONS: dict[str, tuple[str, ...]] = {
    # 3 つの timezone を 1 つに統合。統合元同士も一致していなければならない
    # (異なる値だった場合、統合はいずれかの設定を黙って捨てることになる)。
    "timezone": (
        "schedule.timezone",
        "news_collection.timezone",
        "economic_calendar.fetch_timezone",
    ),
    "embedding.provider": ("rag.embedding_provider",),
    "embedding.model": ("rag.embedding_model",),
    "embedding.base_url": ("rag.embedding_base_url",),
}

EXPECTED_REMOVED = {old for olds in MIGRATIONS.values() for old in olds}
EXPECTED_ADDED = set(MIGRATIONS)


def check_migrated_values(before: dict, after: dict) -> list[str]:
    """移設元と移設先の値が一致することを検証する。

    旧キーがスナップショットに無い場合 (既に移行済みの config 同士の比較) は
    検証をスキップする。
    """
    errors = []
    for new_key, old_keys in MIGRATIONS.items():
        present = {k: before[k] for k in old_keys if k in before}
        if not present:
            continue  # 移行前スナップショットではない

        # 統合元が複数ある場合、それら同士の一致を先に確認する
        distinct = set(present.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(present.items()))
            errors.append(
                f"{new_key}: source keys disagree before migration ({detail}). "
                f"Consolidating them would silently discard one of the settings. "
                f"Decide which value to keep before migrating."
            )
            continue

        old_value = distinct.pop()
        new_value = after.get(new_key)
        if new_value is None:
            errors.append(f"{new_key}: missing in the new config (expected {old_value}).")
        elif new_value != old_value:
            errors.append(
                f"{new_key}: value changed during migration "
                f"({', '.join(sorted(old_keys))} was {old_value}, now {new_value})."
            )
    return errors


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

    # 移設した値そのものが正しく引き継がれているかを検証する。
    # removed/added の許可だけでは、移設中に値が化けても素通りしてしまう。
    value_errors = check_migrated_values(before, after)
    if value_errors:
        print("\n=== MIGRATED VALUE MISMATCH ===")
        for msg in value_errors:
            print(f"  ! {msg}")
        problems += len(value_errors)

    if problems:
        print(
            f"\nFAIL: {problems} problem(s) found. "
            "Unexpected key differences usually mean a block was dropped or "
            "misspelled during the split (values fell back to schema defaults). "
            "Value mismatches mean a migrated setting did not carry over correctly."
        )
        return 1

    print("\nOK: only the expected migration differences were found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
