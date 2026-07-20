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
    strategy.yaml がリストとして書かれると trading/orchestrator が
    全て既定値に落ちるため、fail-fast にする。
    """
    _write(tmp_path, "strategy.yaml", "- item1\n- item2\n")

    with pytest.raises(ConfigError, match="strategy.yaml"):
        _merge_split_configs({}, tmp_path)


def test_merge_wraps_yaml_syntax_error(tmp_path):
    """YAML 構文エラーは生 traceback ではなく ConfigError にラップする。"""
    _write(tmp_path, "llm.yaml", "llm:\n  provider: [unclosed\n")

    with pytest.raises(ConfigError, match="llm.yaml"):
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


REQUIRED_SAMPLE = {
    "settings.yaml": "mode: paper\ntimezone: \"Asia/Tokyo\"\n",
    "llm.yaml": "llm:\n  provider: llamacpp\n",
    "strategy.yaml": "trading:\n  risk_per_trade: 0.02\n",
}


def test_missing_required_file_rejected(tmp_path):
    """strategy.yaml の欠損は ConfigError (既定値での fail-open を防ぐ)。"""
    from src.config.migration import check_required_files

    _write(tmp_path, "settings.yaml", REQUIRED_SAMPLE["settings.yaml"])
    _write(tmp_path, "llm.yaml", REQUIRED_SAMPLE["llm.yaml"])
    # strategy.yaml を置かない

    with pytest.raises(ConfigError, match="strategy.yaml"):
        check_required_files(tmp_path)


def test_empty_required_file_rejected(tmp_path):
    """空ファイルも欠損と同じく ConfigError。"""
    from src.config.migration import check_required_files

    for name, text in REQUIRED_SAMPLE.items():
        _write(tmp_path, name, text)
    _write(tmp_path, "llm.yaml", "")

    with pytest.raises(ConfigError, match="llm.yaml"):
        check_required_files(tmp_path)


def test_legacy_agents_yaml_rejected(tmp_path):
    """残存する agents.yaml を検出する。

    マージ対象から外れているため放置すると黙って無視され、
    ユーザーが編集し続けても何も起きない。
    """
    from src.config.migration import check_required_files

    for name, text in REQUIRED_SAMPLE.items():
        _write(tmp_path, name, text)
    _write(tmp_path, "agents.yaml", "agents:\n  planner:\n    model: x\n")

    with pytest.raises(ConfigError, match="agents.yaml"):
        check_required_files(tmp_path)


def test_all_required_files_present(tmp_path):
    """必要なファイルが揃っていれば通る。"""
    from src.config.migration import check_required_files

    for name, text in REQUIRED_SAMPLE.items():
        _write(tmp_path, name, text)

    check_required_files(tmp_path)  # raises しない


def test_top_level_timezone_and_embedding(tmp_path):
    """新スキーマ: top-level timezone と embedding ブロックが読める。"""
    from src.config import load_config

    _write(tmp_path, "settings.yaml",
           'mode: paper\npaper_provider: yfinance\ntimezone: "Europe/London"\n')
    _write(tmp_path, "llm.yaml",
           'llm:\n'
           '  provider: llamacpp\n'
           '  provider_config:\n'
           '    base_url: "http://localhost:8080/v1"\n'
           '  news_analysis:\n    model: m1\n'
           '  price_analysis:\n    model: m2\n'
           '  reflection:\n    model: m3\n'
           'embedding:\n'
           '  provider: llamacpp\n'
           '  model: nomic-embed-text\n'
           '  base_url: "http://localhost:8080/v1"\n')
    _write(tmp_path, "strategy.yaml", 'trading:\n  risk_per_trade: 0.03\n')

    config = load_config(tmp_path / "settings.yaml")

    assert config.timezone == "Europe/London"
    assert config.embedding.provider == "llamacpp"
    assert config.embedding.model == "nomic-embed-text"
    assert config.embedding.base_url == "http://localhost:8080/v1"
    assert not hasattr(config.schedule, "timezone")
    assert not hasattr(config.rag, "embedding_provider")


def test_timezone_is_required(tmp_path):
    """移行リリース限定: timezone 未指定は既定値に落とさず ConfigError。

    既定 Asia/Tokyo への静かな転落は、Asia/Tokyo 以外を設定していた
    ホストで全時刻判定をずらす (finance_orchestrator_zero_plans_tz_bug と同型)。
    """
    from src.config import load_config

    _write(tmp_path, "settings.yaml", "mode: paper\npaper_provider: yfinance\n")
    _write(tmp_path, "llm.yaml",
           'llm:\n  provider: llamacpp\n'
           '  provider_config:\n    base_url: "http://x/v1"\n'
           '  news_analysis:\n    model: m1\n'
           '  price_analysis:\n    model: m2\n'
           '  reflection:\n    model: m3\n')
    _write(tmp_path, "strategy.yaml", "trading: {}\n")

    with pytest.raises(ConfigError, match="timezone"):
        load_config(tmp_path / "settings.yaml")


def test_embedding_provider_value_validated(tmp_path):
    """未知の embedding provider は fail-fast。

    embedder.py は「llamacpp 以外は全て Ollama」で分岐するため、
    typo が誤ったプロトコルへの接続として黙って通ってしまう。
    """
    from src.config import load_config

    _write(tmp_path, "settings.yaml", 'mode: paper\ntimezone: "Asia/Tokyo"\n')
    _write(tmp_path, "llm.yaml",
           'llm:\n  provider: llamacpp\n'
           '  provider_config:\n    base_url: "http://x/v1"\n'
           '  news_analysis:\n    model: m1\n'
           '  price_analysis:\n    model: m2\n'
           '  reflection:\n    model: m3\n'
           'embedding:\n  provider: ollamaa\n  base_url: "http://x"\n')
    _write(tmp_path, "strategy.yaml", "trading: {}\n")

    with pytest.raises(ConfigError, match="ollamaa"):
        load_config(tmp_path / "settings.yaml")


def test_duplicate_key_within_same_file_rejected(tmp_path):
    """同一ファイル内の重複 top-level キーを拒否する。

    yaml.safe_load は重複キーを無警告で後勝ち上書きする。分割作業中に
    ブロックを二重記載すると、片方の設定 (リスクパラメータを含む) が
    黙って消えるため fail-fast にする。ファイル間の重複を見る
    _check_duplicate_blocks は、読み込み後の dict を扱うため
    この経路を検出できない。
    """
    from src.config import load_config

    _write(tmp_path, "settings.yaml", 'mode: paper\ntimezone: "Asia/Tokyo"\n')
    _write(tmp_path, "llm.yaml",
           'llm:\n  provider: llamacpp\n'
           '  provider_config:\n    base_url: "http://x/v1"\n'
           '  news_analysis:\n    model: m1\n'
           '  price_analysis:\n    model: m2\n'
           '  reflection:\n    model: m3\n'
           'embedding:\n  provider: llamacpp\n  base_url: "http://x/v1"\n')
    _write(tmp_path, "strategy.yaml",
           "trading:\n  risk_per_trade: 0.02\ntrading:\n  risk_per_trade: 0.99\n")

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(tmp_path / "settings.yaml")


def test_duplicate_subkey_within_block_rejected(tmp_path):
    """ネストしたサブキーの重複も拒否する。"""
    from src.config import load_config

    _write(tmp_path, "settings.yaml", 'mode: paper\ntimezone: "Asia/Tokyo"\n')
    _write(tmp_path, "llm.yaml",
           'llm:\n  provider: llamacpp\n'
           '  provider_config:\n    base_url: "http://x/v1"\n'
           '  news_analysis:\n    model: m1\n'
           '  price_analysis:\n    model: m2\n'
           '  reflection:\n    model: m3\n'
           'embedding:\n  provider: llamacpp\n  base_url: "http://x/v1"\n')
    _write(tmp_path, "strategy.yaml",
           "trading:\n  risk_per_trade: 0.02\n  risk_per_trade: 0.99\n")

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(tmp_path / "settings.yaml")
