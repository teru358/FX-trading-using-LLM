"""startup_checks の provider 別必要モデル判定のテスト。

新スキーマ: llm.provider が単一、3 役割すべて同じ provider を共有。
embedding は config.embedding (llm.yaml) で別管理 (LLM と独立)。
実際の HTTP チェックは省略し、モデルリスト計算ロジックのみ検証する。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.startup import (
    _check_approval_gate_config,
    _collect_llm_role_entries,
    _llamacpp_required_models,
    _ollama_required_models,
)


def _make_config(
    *,
    provider: str = "ollama",
    base_url: str = "http://localhost:11434",
    news_model: str = "llama3.1:8b",
    price_model: str = "plutus",
    reflection_model: str = "deepseek-r1:8b",
    embedding_provider: str = "ollama",
    embedding_model: str = "nomic-embed-text",
    embedding_base_url: str = "http://localhost:11434",
):
    """テスト用 config を組み立てる (新スキーマ準拠)。"""
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=provider,
            provider_config=SimpleNamespace(base_url=base_url, command=""),
            news_analysis=SimpleNamespace(model=news_model),
            price_analysis=SimpleNamespace(model=price_model),
            reflection=SimpleNamespace(model=reflection_model),
        ),
        embedding=SimpleNamespace(
            provider=embedding_provider,
            model=embedding_model,
            base_url=embedding_base_url,
        ),
    )


def test_ollama_required_models_all_ollama():
    """provider=ollama → 3 役割 + embedding が必要モデルに集約される。"""
    config = _make_config(
        provider="ollama",
        news_model="gemma3:4b",
        price_model="plutus",
        reflection_model="deepseek-r1:8b",
    )
    models = _ollama_required_models(config)
    assert models == {"gemma3:4b", "plutus", "deepseek-r1:8b", "nomic-embed-text"}


def test_ollama_required_models_llamacpp_provider():
    """provider=llamacpp + embedding=llamacpp → ollama 必要モデルは空。"""
    config = _make_config(
        provider="llamacpp",
        base_url="http://localhost:8080/v1",
        embedding_provider="llamacpp",
        embedding_base_url="http://localhost:8080/v1",
    )
    assert _ollama_required_models(config) == set()


def test_llamacpp_required_models_all_llamacpp():
    """provider=llamacpp → 全モデルが llamacpp 側で必要。"""
    config = _make_config(
        provider="llamacpp",
        base_url="http://localhost:8080/v1",
        news_model="llama3.1-8b",
        price_model="plutus",
        reflection_model="deepseek-r1-8b",
        embedding_provider="llamacpp",
        embedding_base_url="http://localhost:8080/v1",
    )
    assert _llamacpp_required_models(config) == {
        "llama3.1-8b", "plutus", "deepseek-r1-8b", "nomic-embed-text",
    }


def test_llamacpp_required_models_ollama_provider():
    """provider=ollama → llamacpp 必要モデルは空 (embedding も ollama)。"""
    config = _make_config()
    assert _llamacpp_required_models(config) == set()


def test_provider_and_embedding_split():
    """LLM provider と embedding provider は独立。

    LLM=claude-cli + embedding=ollama → ollama 必要モデルは embedding のみ。
    """
    config = _make_config(
        provider="claude-cli",
        base_url="",  # claude-cli では未使用
        news_model="claude-haiku-4-5",
        price_model="claude-sonnet-4-6",
        reflection_model="claude-haiku-4-5",
        embedding_provider="ollama",
        embedding_base_url="http://localhost:11434",
    )
    assert _ollama_required_models(config) == {"nomic-embed-text"}
    assert _llamacpp_required_models(config) == set()


def test_api_provider_with_llamacpp_embedding():
    """LLM=gemini + embedding=llamacpp → llamacpp は embedding のみ。"""
    config = _make_config(
        provider="gemini",
        base_url="",
        news_model="gemini-2.0-flash",
        price_model="gemini-2.0-flash",
        reflection_model="gemini-2.0-flash",
        embedding_provider="llamacpp",
        embedding_base_url="http://localhost:8080/v1",
    )
    assert _ollama_required_models(config) == set()
    assert _llamacpp_required_models(config) == {"nomic-embed-text"}


def test_collect_llm_role_entries_order_and_labels():
    """表示用エントリは news → price → reflect → embed の順で短縮ラベル化される。

    新スキーマでは LLM 3 役割が同じ provider を共有し、embedding だけ独立。
    """
    config = _make_config(
        provider="llamacpp",
        base_url="http://localhost:8080/v1",
        news_model="llama3.1-8b",
        price_model="plutus",
        reflection_model="deepseek-r1-8b",
        embedding_provider="ollama",
        embedding_base_url="http://localhost:11434",
    )
    entries = _collect_llm_role_entries(config)
    assert [e[0] for e in entries] == ["news", "price", "reflect", "embed"]
    assert entries[0] == ("news", "llamacpp", "llama3.1-8b")
    assert entries[1] == ("price", "llamacpp", "plutus")
    assert entries[2] == ("reflect", "llamacpp", "deepseek-r1-8b")
    assert entries[3] == ("embed", "ollama", "nomic-embed-text")


def test_collect_llm_role_entries_unset_model_shown():
    """model 空欄の役割は '(unset)' として表示される (loader でエラーになる前のフォールバック表示)。"""
    config = _make_config(
        provider="ollama",
        news_model="",
        price_model="plutus",
        reflection_model="deepseek-r1:8b",
    )
    entries = _collect_llm_role_entries(config)
    news_entry = next(e for e in entries if e[0] == "news")
    assert news_entry == ("news", "ollama", "(unset)")


def _make_gate_config(*, approval_gate: bool, api_enabled: bool):
    """approval gate 検証用の最小 config。"""
    return SimpleNamespace(
        orchestrator=SimpleNamespace(approval_gate=approval_gate),
        api=SimpleNamespace(enabled=api_enabled),
    )


def test_approval_gate_requires_api_enabled(monkeypatch):
    """approval_gate=True で api.enabled=False なら起動時エラー。"""
    monkeypatch.setenv("API_SECRET_KEY", "secret")
    config = _make_gate_config(approval_gate=True, api_enabled=False)
    ok, err = _check_approval_gate_config(config)
    assert ok is False
    assert err is not None


def test_approval_gate_requires_api_key(monkeypatch):
    """approval_gate=True かつ api.enabled=True でも API_SECRET_KEY 未設定ならエラー。"""
    monkeypatch.delenv("API_SECRET_KEY", raising=False)
    config = _make_gate_config(approval_gate=True, api_enabled=True)
    ok, err = _check_approval_gate_config(config)
    assert ok is False
    assert err is not None


def test_approval_gate_ok_when_configured(monkeypatch):
    """approval_gate=True で api.enabled=True かつ API_SECRET_KEY 設定済みなら OK。"""
    monkeypatch.setenv("API_SECRET_KEY", "secret")
    config = _make_gate_config(approval_gate=True, api_enabled=True)
    ok, err = _check_approval_gate_config(config)
    assert ok is True
    assert err is None


def test_approval_gate_off_skips_check(monkeypatch):
    """approval_gate=False (既定) なら api.enabled / API_SECRET_KEY を問わず OK。"""
    monkeypatch.delenv("API_SECRET_KEY", raising=False)
    config = _make_gate_config(approval_gate=False, api_enabled=False)
    ok, err = _check_approval_gate_config(config)
    assert ok is True
    assert err is None
