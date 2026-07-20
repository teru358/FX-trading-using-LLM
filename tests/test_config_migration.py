"""config 再編成 (2026-07-20) の移行検出機構のテスト。

spec: docs/superpowers/specs/2026-07-20-config-file-reorganization-design.md
"""
from pathlib import Path

import pytest

from src.config.loader import ConfigError, _merge_split_configs


def _write(dirpath: Path, name: str, text: str) -> None:
    (dirpath / name).write_text(text, encoding="utf-8")


def test_merge_rejects_non_mapping_file(tmp_path):
    """分割ファイルが mapping でない場合、黙ってスキップせず ConfigError。

    旧実装は isinstance(extra, dict) false で silent skip していた。
    Task 4 以降は strategy.yaml がリストとして書かれると
    trading/orchestrator が全て既定値に落ちるため、fail-fast にする。

    ファイル名が instruments.yaml なのは、この時点の SPLIT_CONFIG_FILES に
    含まれるファイルで検証する必要があるため (llm.yaml/strategy.yaml への
    切替は Task 4)。検証対象は非 mapping 分岐でありファイル名に依存しない。
    """
    _write(tmp_path, "instruments.yaml", "- item1\n- item2\n")

    with pytest.raises(ConfigError, match="instruments.yaml"):
        _merge_split_configs({}, tmp_path)


def test_merge_wraps_yaml_syntax_error(tmp_path):
    """YAML 構文エラーは生 traceback ではなく ConfigError にラップする。

    ファイル名の選択理由は test_merge_rejects_non_mapping_file と同じ。
    """
    _write(tmp_path, "news_sources.yaml", "keywords:\n  fx: [unclosed\n")

    with pytest.raises(ConfigError, match="news_sources.yaml"):
        _merge_split_configs({}, tmp_path)


def test_merge_accepts_missing_file(tmp_path):
    """ファイル不在は正常 (存在チェックは Task 4 の必須ファイル検証が担当)。"""
    result = _merge_split_configs({"mode": "paper"}, tmp_path)
    assert result == {"mode": "paper"}
