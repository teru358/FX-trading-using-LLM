"""config 再編成 (2026-07-20) の移行検出。

spec: docs/superpowers/specs/2026-07-20-config-file-reorganization-design.md §3.4-3.6

検査はマージ「前」に、各ファイルの生 dict に対して個別に行う。マージ後の dict を
見ると、ブロック全置換により旧キーが他ファイルのブロックに覆い隠されて消えるため、
移行未完了を検出できない (§3.6)。
"""
from __future__ import annotations

import difflib
from pathlib import Path

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


# YAML の top-level に現れうるキー。AppConfig のフィールド名と一致しないものが
# あるため (notification → notifier 等)、YAML 側の名前で定義する。
KNOWN_TOP_LEVEL_KEYS = frozenset({
    # settings.yaml
    "mode", "paper_provider", "live_broker", "timezone",
    "logging", "api", "notification", "data_backup",
    # llm.yaml
    "llm", "agents", "embedding",
    # strategy.yaml
    "trading", "price_monitor", "schedule", "analysis",
    "news_collection", "economic_calendar", "orchestrator", "rag",
    "weekly_diagnosis",
    # instruments.yaml / news_sources.yaml
    "instruments", "keywords", "news_sources",
})


def _check_unknown_top_level(files: dict[str, dict]) -> list[str]:
    """未知の top-level キー (ブロック名の typo) を検出する。

    サブキーには適用しない。top-level ブロック名の間違いはブロック全体を
    既定値に落とすため、無関係な未知キーとは区別して扱う。
    """
    errors = []
    for fname, data in files.items():
        for key in data:
            if key in KNOWN_TOP_LEVEL_KEYS:
                continue
            close = difflib.get_close_matches(key, KNOWN_TOP_LEVEL_KEYS, n=1, cutoff=0.7)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            errors.append(
                f"unknown top-level key '{key}' in {fname}.{hint} "
                f"Known keys: {', '.join(sorted(KNOWN_TOP_LEVEL_KEYS))}"
            )
    return errors


# 欠損を許容できないファイル。llm.yaml は llm.provider/model が必須のため
# 既定値では起動せず、strategy.yaml の欠損は trading/orchestrator の
# 無警告な既定値化 (fail-open) を招く。
REQUIRED_CONFIG_FILES = ("llm.yaml", "strategy.yaml")

# 移行により廃止されたファイル。残っていてもマージされないため黙殺される。
OBSOLETE_CONFIG_FILES = ("agents.yaml",)


def check_required_files(config_dir: Path) -> None:
    """必須ファイルの存在と、廃止ファイルの不在を検証する。"""
    errors = []

    for fname in REQUIRED_CONFIG_FILES:
        fpath = config_dir / fname
        if not fpath.exists():
            errors.append(
                f"{fname} is required but not found in {config_dir}. "
                f"Copy {fname}.example and edit it. {_MIGRATION_TAG}"
            )
        elif not fpath.read_text(encoding="utf-8").strip():
            errors.append(
                f"{fname} is empty. It must define its configuration blocks; "
                f"an empty file would silently fall back to schema defaults. "
                f"{_MIGRATION_TAG}"
            )

    for fname in OBSOLETE_CONFIG_FILES:
        if (config_dir / fname).exists():
            errors.append(
                f"{fname} is obsolete and no longer read. Its contents moved to "
                f"llm.yaml (agents: block). Merge and delete it. {_MIGRATION_TAG}"
            )

    if errors:
        raise ConfigError(
            "config migration required:\n  - " + "\n  - ".join(errors)
        )


def check_migration(files: dict[str, dict]) -> None:
    """移行検出をまとめて実行する。違反があれば全件を 1 つの ConfigError で報告。

    Args:
        files: {ファイル名: 生 dict}。マージ前の各ファイルの内容。
    """
    errors = (
        _check_denylist(files)
        + _check_duplicate_blocks(files)
        + _check_unknown_top_level(files)
    )
    if errors:
        raise ConfigError(
            "config migration required:\n  - " + "\n  - ".join(errors)
        )
