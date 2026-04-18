"""startup_checks の provider 別必要モデル判定のテスト。

実際の HTTP チェックは省略し、モデルリスト計算ロジックのみ検証する。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.startup import (
    _collect_llm_role_entries,
    _llamacpp_required_models,
    _ollama_required_models,
)


def _make_config(
    embedding_provider: str = "ollama",
    embedding_model: str = "nomic-embed-text",
    news: tuple[str, str] = ("ollama", ""),
    price: tuple[str, str] = ("ollama", ""),
    reflection: tuple[str, str] = ("ollama", ""),
):
    """テスト用 config を組み立てる。"""
    return SimpleNamespace(
        llm=SimpleNamespace(
            ollama=SimpleNamespace(base_url="http://localhost:11434"),
            llamacpp=SimpleNamespace(base_url="http://localhost:8080/v1"),
            news_analysis=SimpleNamespace(provider=news[0], model=news[1]),
            price_analysis=SimpleNamespace(provider=price[0], model=price[1]),
            reflection=SimpleNamespace(provider=reflection[0], model=reflection[1]),
        ),
        rag=SimpleNamespace(
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        ),
    )


def test_ollama_required_models_all_ollama_no_overrides():
    """全ロール ollama + モデル未指定 → デフォルト + embedding を含む。"""
    config = _make_config()
    models = _ollama_required_models(config)
    assert "llama3.1:8b" in models  # _DEFAULT_OLLAMA_MODEL
    assert "nomic-embed-text" in models


def test_ollama_required_models_all_ollama_explicit_overrides():
    """明示モデル指定時はそれがチェック対象。"""
    config = _make_config(
        news=("ollama", "gemma3:4b"),
        price=("ollama", "plutus"),
        reflection=("ollama", "deepseek-r1:8b"),
    )
    models = _ollama_required_models(config)
    assert models == {"gemma3:4b", "plutus", "deepseek-r1:8b", "nomic-embed-text"}


def test_ollama_required_models_all_llamacpp():
    """全ロール llamacpp → Ollama 必要モデルは空。"""
    config = _make_config(
        embedding_provider="llamacpp",
        news=("llamacpp", "llama3.1-8b"),
        price=("llamacpp", "plutus"),
        reflection=("llamacpp", "deepseek-r1-8b"),
    )
    assert _ollama_required_models(config) == set()


def test_llamacpp_required_models_all_llamacpp():
    """全ロール llamacpp → 全モデルが必要。"""
    config = _make_config(
        embedding_provider="llamacpp",
        news=("llamacpp", "llama3.1-8b"),
        price=("llamacpp", "plutus"),
        reflection=("llamacpp", "deepseek-r1-8b"),
    )
    assert _llamacpp_required_models(config) == {
        "llama3.1-8b", "plutus", "deepseek-r1-8b", "nomic-embed-text",
    }


def test_llamacpp_required_models_all_ollama():
    """全ロール ollama → llamacpp 必要モデルは空。"""
    config = _make_config()
    assert _llamacpp_required_models(config) == set()


def test_mixed_providers():
    """ollama + llamacpp 混合 → 両方のチェック対象が分離される。"""
    config = _make_config(
        embedding_provider="llamacpp",
        news=("llamacpp", "llama3.1-8b"),
        price=("ollama", "plutus:latest"),
        reflection=("claude", "claude-haiku"),  # 別プロバイダー
    )
    assert _ollama_required_models(config) == {"plutus:latest"}
    assert _llamacpp_required_models(config) == {"llama3.1-8b", "nomic-embed-text"}


def test_api_providers_are_skipped_from_both():
    """gemini/openai/claude は Ollama/llamacpp の両チェックから除外される。"""
    config = _make_config(
        embedding_provider="llamacpp",
        news=("gemini", "gemini-2.0-flash"),
        price=("openai", "gpt-4o-mini"),
        reflection=("claude", "claude-haiku"),
    )
    assert _ollama_required_models(config) == set()
    # embedding のみ llamacpp
    assert _llamacpp_required_models(config) == {"nomic-embed-text"}


def test_collect_llm_role_entries_order_and_labels():
    """表示用エントリは news → price → reflect → embed の順で短縮ラベル化される。"""
    config = _make_config(
        embedding_provider="llamacpp",
        news=("llamacpp", "llama3.1-8b"),
        price=("llamacpp", "plutus"),
        reflection=("ollama", "deepseek-r1:8b"),
    )
    entries = _collect_llm_role_entries(config)
    assert [e[0] for e in entries] == ["news", "price", "reflect", "embed"]
    assert entries[0] == ("news", "llamacpp", "llama3.1-8b")
    assert entries[1] == ("price", "llamacpp", "plutus")
    assert entries[2] == ("reflect", "ollama", "deepseek-r1:8b")
    assert entries[3] == ("embed", "llamacpp", "nomic-embed-text")


def test_collect_llm_role_entries_ollama_default_model():
    """Ollama で model 未指定 → _DEFAULT_OLLAMA_MODEL がフォールバックとして表示される。"""
    config = _make_config(news=("ollama", ""))
    entries = _collect_llm_role_entries(config)
    news_entry = next(e for e in entries if e[0] == "news")
    assert news_entry == ("news", "ollama", "llama3.1:8b")
