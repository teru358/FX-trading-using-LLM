from __future__ import annotations

from src.config import AppConfig
from src.llm.claude_cli_client import ClaudeCliClient
from src.llm.claude_client import ClaudeClient
from src.llm.client import LLMClient
from src.llm.gemini_client import GeminiClient
from src.llm.llamacpp_client import LlamaCppClient
from src.llm.ollama_client import OllamaClient
from src.llm.openai_client import OpenAIClient

# 有効なロール名
LLM_ROLES = ("news_analysis", "price_analysis", "reflection")


def create_llm_client(config: AppConfig, role: str) -> LLMClient:
    """ロールに応じた LLMClient を生成する。

    config.llm.provider と config.llm.provider_config を共通で使い、
    config.llm.<role>.model だけを役割ごとに切り替える。

    role:
        "news_analysis"  — ニュース感情分析
        "price_analysis" — テクニカル価格分析
        "reflection"     — 振り返り生成

    プロバイダー (config.llm.provider):
        "ollama"     — ローカル Ollama
        "llamacpp"   — ローカル llama.cpp (llama-swap 経由)
        "claude-cli" — Claude Code CLI (`claude -p`) 経由、サブスクプラン利用
        "gemini"     — Google Gemini API
        "openai"     — OpenAI API
        "claude"     — Anthropic Claude API (API キー利用)
    """
    if role not in LLM_ROLES:
        raise ValueError(f"unknown LLM role '{role}', expected one of {LLM_ROLES}")

    provider = config.llm.provider
    pc = config.llm.provider_config
    role_cfg = getattr(config.llm, role)
    model = role_cfg.model

    if provider == "ollama":
        return OllamaClient(
            base_url=pc.base_url,
            model=model,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
        )

    if provider == "llamacpp":
        return LlamaCppClient(
            base_url=pc.base_url,
            model=model,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
        )

    if provider == "claude-cli":
        return ClaudeCliClient(
            model=model,
            command=pc.command,
            isolated_cwd=pc.isolated_cwd or None,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
            extra_args=list(pc.extra_args),
        )

    if provider == "gemini":
        return GeminiClient(
            model=model,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
        )

    if provider == "openai":
        return OpenAIClient(
            model=model,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
        )

    if provider == "claude":
        return ClaudeClient(
            model=model,
            timeout_seconds=pc.timeout_seconds,
            max_retries=pc.max_retries,
            max_tokens=pc.max_tokens,
        )

    raise ValueError(f"unsupported llm.provider '{provider}'")
