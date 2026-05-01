"""LLM 設定の起動時バリデーションテスト。

新スキーマ:
  llm:
    provider: <name>
    provider_config: { base_url, command, ... }
    news_analysis: { model, temperature }
    price_analysis: { model, temperature }
    reflection: { model, temperature }

旧形式 (llm.ollama / llm.<role>.provider) は ConfigError で起動阻止される。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from src.config import BASE_DIR, ConfigError, load_config


def _write_settings(tmp_path: Path, overrides: dict) -> Path:
    """本番 config/ を tmp_path にコピーし、settings.yaml にパッチを当てる。"""
    src_config_dir = BASE_DIR / "config"
    dst_config_dir = tmp_path / "config"
    shutil.copytree(src_config_dir, dst_config_dir)

    settings_path = dst_config_dir / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    _deep_merge(data, overrides)
    settings_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return settings_path


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ── provider 別必須フィールド ────────────────────────────────


def test_ollama_requires_base_url(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "ollama",
            "provider_config": {"base_url": ""},
            "news_analysis":  {"model": "llama3.1:8b"},
            "price_analysis": {"model": "llama3.1:8b"},
            "reflection":     {"model": "llama3.1:8b"},
        }
    })
    with pytest.raises(ConfigError, match="base_url is required"):
        load_config(settings_path)


def test_llamacpp_requires_base_url(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "llamacpp",
            "provider_config": {"base_url": ""},
            "news_analysis":  {"model": "llama3.1-8b"},
            "price_analysis": {"model": "plutus"},
            "reflection":     {"model": "deepseek-r1-8b"},
        }
    })
    with pytest.raises(ConfigError, match="base_url is required"):
        load_config(settings_path)


def test_claude_cli_does_not_require_base_url(tmp_path):
    """claude-cli は base_url 不要。command 空欄なら 'claude' 補完。"""
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "claude-cli",
            "provider_config": {"base_url": "", "command": ""},
            "news_analysis":  {"model": "claude-haiku-4-5"},
            "price_analysis": {"model": "claude-sonnet-4-6"},
            "reflection":     {"model": "claude-haiku-4-5"},
        }
    })
    cfg = load_config(settings_path)
    assert cfg.llm.provider == "claude-cli"
    assert cfg.llm.provider_config.command == "claude"  # 自動補完


def test_claude_api_max_tokens_default(tmp_path):
    """claude (API) の max_tokens が 0 なら 4096 補完。"""
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "claude",
            "provider_config": {"max_tokens": 0},
            "news_analysis":  {"model": "claude-haiku-4-5-20251001"},
            "price_analysis": {"model": "claude-haiku-4-5-20251001"},
            "reflection":     {"model": "claude-haiku-4-5-20251001"},
        }
    })
    cfg = load_config(settings_path)
    assert cfg.llm.provider_config.max_tokens == 4096


# ── role 別 model 必須 ───────────────────────────────────────


def test_role_model_required(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "ollama",
            "provider_config": {"base_url": "http://localhost:11434"},
            "news_analysis":  {"model": ""},  # 空欄
            "price_analysis": {"model": "llama3.1:8b"},
            "reflection":     {"model": "llama3.1:8b"},
        }
    })
    with pytest.raises(ConfigError, match="news_analysis.model is required"):
        load_config(settings_path)


# ── 不明な provider ─────────────────────────────────────────


def test_unknown_provider_rejected(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "bogus",
            "provider_config": {"base_url": "http://localhost:11434"},
            "news_analysis":  {"model": "x"},
            "price_analysis": {"model": "x"},
            "reflection":     {"model": "x"},
        }
    })
    with pytest.raises(ConfigError, match="Unknown llm.provider"):
        load_config(settings_path)


# ── 旧形式の検出 ────────────────────────────────────────────


def test_legacy_ollama_subsection_rejected(tmp_path):
    """llm.ollama: { base_url: ... } が残っていたら ConfigError。"""
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "ollama",
            "provider_config": {"base_url": "http://localhost:11434"},
            "ollama": {"base_url": "http://localhost:11434"},  # 旧形式
            "news_analysis":  {"model": "llama3.1:8b"},
            "price_analysis": {"model": "llama3.1:8b"},
            "reflection":     {"model": "llama3.1:8b"},
        }
    })
    with pytest.raises(ConfigError, match="Legacy LLM config detected"):
        load_config(settings_path)


def test_legacy_per_role_provider_rejected(tmp_path):
    """llm.<role>.provider が残っていたら ConfigError。"""
    settings_path = _write_settings(tmp_path, {
        "llm": {
            "provider": "ollama",
            "provider_config": {"base_url": "http://localhost:11434"},
            "news_analysis":  {"model": "x", "provider": "ollama"},  # 旧形式
            "price_analysis": {"model": "x"},
            "reflection":     {"model": "x"},
        }
    })
    with pytest.raises(ConfigError, match="Legacy per-role 'provider'"):
        load_config(settings_path)


# ── embedding 設定 ──────────────────────────────────────────


def test_embedding_ollama_requires_base_url(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "rag": {
            "embedding_provider": "ollama",
            "embedding_base_url": "",
        }
    })
    with pytest.raises(ConfigError, match="embedding_base_url is required"):
        load_config(settings_path)


def test_embedding_llamacpp_requires_base_url(tmp_path):
    settings_path = _write_settings(tmp_path, {
        "rag": {
            "embedding_provider": "llamacpp",
            "embedding_base_url": "",
        }
    })
    with pytest.raises(ConfigError, match="embedding_base_url is required"):
        load_config(settings_path)
