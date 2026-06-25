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
