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
