"""config 再編成 (2026-07-20) の移行検出。

spec: docs/superpowers/specs/2026-07-20-config-file-reorganization-design.md §3.4-3.6

検査はマージ「前」に、各ファイルの生 dict に対して個別に行う。マージ後の dict を
見ると、ブロック全置換により旧キーが他ファイルのブロックに覆い隠されて消えるため、
移行未完了を検出できない (§3.6)。
"""
from __future__ import annotations


from src.config.errors import ConfigError

# (ブロック名, 旧キー, 移動先の説明)
_MIGRATED_KEYS = [
    ("schedule", "timezone", "top-level 'timezone' in settings.yaml"),
    ("news_collection", "timezone", "top-level 'timezone' in settings.yaml"),
    ("economic_calendar", "fetch_timezone", "top-level 'timezone' in settings.yaml"),
    ("rag", "embedding_provider", "'embedding.provider' in llm.yaml"),
    ("rag", "embedding_model", "'embedding.model' in llm.yaml"),
    ("rag", "embedding_base_url", "'embedding.base_url' in llm.yaml"),
]

_MIGRATION_TAG = "(config migration 2026-07-20)"


def _check_denylist(files: dict[str, dict]) -> list[str]:
    """各ファイルに旧キーが残っていないか検査し、違反メッセージを返す。"""
    errors = []
    for fname, data in files.items():
        for block, key, destination in _MIGRATED_KEYS:
            section = data.get(block)
            if isinstance(section, dict) and key in section:
                errors.append(
                    f"'{block}.{key}' in {fname} has moved to {destination}. "
                    f"Remove the old key. {_MIGRATION_TAG}"
                )
    return errors


def _check_duplicate_blocks(files: dict[str, dict]) -> list[str]:
    """同一 top-level ブロックが複数ファイルに現れないか検査する。

    マージは後勝ちの全置換なので、重複すると片方の定義が丸ごと無視される。
    """
    seen: dict[str, str] = {}
    errors = []
    for fname, data in files.items():
        for key in data:
            if key in seen:
                errors.append(
                    f"top-level key '{key}' is defined in both {seen[key]} and {fname}. "
                    f"The later file wins and the other definition is silently ignored. "
                    f"Keep each block in exactly one file."
                )
            else:
                seen[key] = fname
    return errors


def check_migration(files: dict[str, dict]) -> None:
    """移行検出をまとめて実行する。違反があれば全件を 1 つの ConfigError で報告。

    Args:
        files: {ファイル名: 生 dict}。マージ前の各ファイルの内容。
    """
    errors = _check_denylist(files) + _check_duplicate_blocks(files)
    if errors:
        raise ConfigError(
            "config migration required:\n  - " + "\n  - ".join(errors)
        )
