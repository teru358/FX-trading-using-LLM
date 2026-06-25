"""factory.create_agent_llm / _build_client のテスト。

実 LLM 接続は起こさない (provider 別 Client インスタンスの型/属性だけ確認)。
"""
from __future__ import annotations

import pytest

from src.config.schema import AppConfig, ProviderConfig
from src.llm.factory import _build_client
from src.llm.llamacpp_client import LlamaCppClient


def test_build_client_llamacpp() -> None:
    pc = ProviderConfig(base_url="http://localhost:8080/v1", timeout_seconds=30)
    client = _build_client("llamacpp", pc, "plutus")
    assert isinstance(client, LlamaCppClient)
    # LlamaCppClient は model を .model_name プロパティで公開する (private は self._model)
    assert client.model_name == "plutus"


def test_agent_llm_bundle_holds_client_and_temperature() -> None:
    from src.config.schema import AgentLlm

    sentinel_client = object()
    bundle = AgentLlm(client=sentinel_client, temperature=0.4)
    assert bundle.client is sentinel_client
    assert bundle.temperature == 0.4


import src.llm.factory as factory_mod
from src.config.schema import AgentLlm


def _fallback_config() -> AppConfig:
    """agent_llms 未設定 (全 fallback) の AppConfig。"""
    cfg = AppConfig()
    # 既定 LLMConfig は3役割の model 空。fallback client 生成のため model を埋める。
    cfg.llm.news_analysis.model = "news-model"
    cfg.llm.price_analysis.model = "price-model"
    cfg.llm.reflection.model = "reflect-model"
    return cfg


@pytest.mark.parametrize(
    "agent_name,expected_role",
    [
        ("planner", "price_analysis"),
        ("news", "news_analysis"),
        ("technical", "price_analysis"),
        ("execution_opinion", "price_analysis"),
        ("context_summary", "reflection"),
    ],
)
def test_create_agent_llm_fallback_maps_to_role(
    monkeypatch, agent_name, expected_role
) -> None:
    """provider 空の agent は既存役割 client + 役割 temperature に落ちる。"""
    captured = {}

    def fake_create_llm_client(config, role):
        captured["role"] = role
        return object()

    monkeypatch.setattr(factory_mod, "create_llm_client", fake_create_llm_client)

    cfg = _fallback_config()
    bundle = factory_mod.create_agent_llm(cfg, agent_name)

    assert isinstance(bundle, AgentLlm)
    assert captured["role"] == expected_role
    # temperature は fallback 役割の temperature
    expected_temp = getattr(cfg.llm, expected_role).temperature
    assert bundle.temperature == expected_temp


def test_create_agent_llm_unknown_agent_raises() -> None:
    cfg = _fallback_config()
    with pytest.raises(ValueError, match="unknown agent"):
        factory_mod.create_agent_llm(cfg, "nonexistent")


from src.config import ConfigError
from src.config.schema import AgentLlmConfig, ProviderConfig


def _config_with_agent(agent_name, agent_cfg: AgentLlmConfig, *, top_provider="claude-cli",
                       provider_configs=None) -> AppConfig:
    cfg = AppConfig()
    cfg.llm.provider = top_provider
    cfg.llm.price_analysis.model = "price-model"
    cfg.llm.news_analysis.model = "news-model"
    cfg.llm.reflection.model = "reflect-model"
    if provider_configs:
        cfg.llm.provider_configs = provider_configs
    setattr(cfg.agent_llms, agent_name, agent_cfg)
    return cfg


def test_create_agent_llm_uses_provider_configs(monkeypatch) -> None:
    """agent provider != top-level → provider_configs[provider] を使い AgentLlm を返す。"""
    captured = {}

    def fake_build_client(provider, pc, model):
        captured.update(provider=provider, pc=pc, model=model)
        return object()

    monkeypatch.setattr(factory_mod, "_build_client", fake_build_client)

    agent_cfg = AgentLlmConfig(provider="llamacpp", model="plutus", temperature=0.05)
    pc = ProviderConfig(base_url="http://localhost:8080/v1")
    cfg = _config_with_agent(
        "technical", agent_cfg, top_provider="claude-cli",
        provider_configs={"llamacpp": pc},
    )

    bundle = factory_mod.create_agent_llm(cfg, "technical")
    assert bundle.temperature == 0.05            # agent 設定の temperature
    assert captured["provider"] == "llamacpp"
    assert captured["model"] == "plutus"
    assert captured["pc"] is pc


def test_create_agent_llm_same_provider_reuses_single_provider_config(monkeypatch) -> None:
    """agent provider == top-level provider → 既存 provider_config を流用。"""
    captured = {}
    monkeypatch.setattr(
        factory_mod, "_build_client",
        lambda provider, pc, model: captured.update(pc=pc) or object(),
    )
    agent_cfg = AgentLlmConfig(provider="claude-cli", model="claude-sonnet-4-6", temperature=0.1)
    cfg = _config_with_agent("planner", agent_cfg, top_provider="claude-cli")
    factory_mod.create_agent_llm(cfg, "planner")
    assert captured["pc"] is cfg.llm.provider_config   # 単一設定を流用


def test_create_agent_llm_missing_provider_config_raises(monkeypatch) -> None:
    """agent provider != top-level かつ provider_configs に該当無し → ConfigError。"""
    agent_cfg = AgentLlmConfig(provider="llamacpp", model="plutus", temperature=0.1)
    cfg = _config_with_agent(
        "technical", agent_cfg, top_provider="claude-cli", provider_configs={},  # 該当無し
    )
    with pytest.raises(ConfigError, match="provider_configs"):
        factory_mod.create_agent_llm(cfg, "technical")
