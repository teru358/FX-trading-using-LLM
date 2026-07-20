"""config 再編成 (2026-07-20) の移行検出機構のテスト。

spec: docs/superpowers/specs/2026-07-20-config-file-reorganization-design.md
"""
from pathlib import Path

import pytest

from src.config.loader import ConfigError, _merge_split_configs
from src.config.migration import check_migration


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


def test_denylist_detects_old_timezone_key():
    """schedule.timezone の残存を検出し、移動先を示す。"""
    files = {"settings.yaml": {"schedule": {"timezone": "Asia/Tokyo"}}}

    with pytest.raises(ConfigError) as exc:
        check_migration(files)

    msg = str(exc.value)
    assert "schedule.timezone" in msg
    assert "timezone" in msg
    assert "settings.yaml" in msg


def test_denylist_reports_all_violations_at_once():
    """複数の旧キーが残っていれば全件まとめて報告する (1つ直すたびの再起動を避ける)。"""
    files = {
        "settings.yaml": {
            "schedule": {"timezone": "Asia/Tokyo"},
            "news_collection": {"timezone": "Asia/Tokyo"},
        },
        "strategy.yaml": {"rag": {"embedding_provider": "ollama"}},
    }

    with pytest.raises(ConfigError) as exc:
        check_migration(files)

    msg = str(exc.value)
    assert "schedule.timezone" in msg
    assert "news_collection.timezone" in msg
    assert "rag.embedding_provider" in msg


def test_denylist_detects_key_masked_by_block_replacement():
    """マージ後検査では見えない masking ケースを検出する。

    settings.yaml の schedule.timezone は、strategy.yaml が schedule ブロックを
    持つとマージ後の dict から消える (ブロック全置換)。移行途中に最も
    起こりやすい状態であり、ここを取りこぼすと検出機構の意味がない。
    """
    files = {
        "settings.yaml": {"schedule": {"timezone": "Asia/Tokyo"}},
        "strategy.yaml": {"schedule": {"technical_trade_interval_minutes": 30}},
    }

    with pytest.raises(ConfigError, match="schedule.timezone"):
        check_migration(files)


def test_duplicate_top_level_block_rejected():
    """同一 top-level ブロックが複数ファイルにあると後勝ちで片方が消えるため拒否。"""
    files = {
        "settings.yaml": {"trading": {"risk_per_trade": 0.02}},
        "strategy.yaml": {"trading": {"risk_per_trade": 0.01}},
    }

    with pytest.raises(ConfigError) as exc:
        check_migration(files)

    msg = str(exc.value)
    assert "trading" in msg
    assert "settings.yaml" in msg
    assert "strategy.yaml" in msg


def test_clean_config_passes():
    """正しく移行された構成では何も起きない。"""
    files = {
        "settings.yaml": {"timezone": "Asia/Tokyo", "mode": "paper"},
        "llm.yaml": {"llm": {"provider": "llamacpp"}, "embedding": {"provider": "ollama"}},
        "strategy.yaml": {"trading": {"risk_per_trade": 0.02}},
    }

    check_migration(files)  # raises しなければ成功


def test_unknown_top_level_key_rejected_with_suggestion():
    """top-level ブロック名の typo を検出し、近い既知キーを提示する。"""
    files = {"strategy.yaml": {"tradng": {"risk_per_trade": 0.02}}}

    with pytest.raises(ConfigError) as exc:
        check_migration(files)

    msg = str(exc.value)
    assert "tradng" in msg
    assert "trading" in msg  # 候補提示


def test_unknown_subkey_is_ignored():
    """サブキーの未知は無視する (検査は top-level 1 階層のみ)。"""
    files = {"strategy.yaml": {"trading": {"totally_unknown_subkey": 1}}}

    check_migration(files)  # raises しない


def test_known_top_level_keys_accepted():
    """AppConfig が受け付ける全 top-level キーが通ること。"""
    files = {
        "settings.yaml": {
            "mode": "paper", "paper_provider": "yfinance", "live_broker": None,
            "timezone": "Asia/Tokyo", "logging": {}, "api": {},
            "notification": {}, "data_backup": {},
        },
        "llm.yaml": {"llm": {}, "agents": {}, "embedding": {}},
        "strategy.yaml": {
            "trading": {}, "price_monitor": {}, "schedule": {}, "analysis": {},
            "news_collection": {}, "economic_calendar": {}, "orchestrator": {},
            "rag": {}, "weekly_diagnosis": {},
        },
        "instruments.yaml": {"instruments": []},
        "news_sources.yaml": {"keywords": {}, "news_sources": {}},
    }

    check_migration(files)  # raises しない
