from __future__ import annotations

from src.config import _DEFAULT_OLLAMA_MODEL
from src.llm.claude_cli_client import ClaudeCliClient
from src.llm.claude_client import ClaudeClient
from src.llm.client import LLMClient
from src.llm.gemini_client import GeminiClient
from src.llm.llamacpp_client import LlamaCppClient
from src.llm.ollama_client import OllamaClient
from src.llm.openai_client import OpenAIClient

# 有効なロール名
LLM_ROLES = ("news_analysis", "price_analysis", "reflection")


def create_llm_client(config, role: str) -> LLMClient:
    """ロールに応じた LLMClient を生成する。

    role:
        "news_analysis"  — ニュース感情分析
        "price_analysis" — テクニカル価格分析
        "reflection"     — 振り返り生成

    プロバイダー:
        "ollama"     — ローカル Ollama（デフォルト）
        "llamacpp"   — ローカル llama.cpp (llama-swap 経由)
        "claude-cli" — Claude Code CLI (`claude -p`) 経由、サブスクプラン利用
        "gemini"     — Google Gemini API
        "openai"     — OpenAI API
        "claude"     — Anthropic Claude API (API キー利用)

    llm.<role>.model が空の場合は各プロバイダーのデフォルトモデルを使用。
    """
    role_cfg = getattr(config.llm, role)
    provider = role_cfg.provider
    model_override = role_cfg.model  # "" = プロバイダーのデフォルトを使用

    if provider == "gemini":
        model = model_override or config.gemini.model
        return GeminiClient(
            model=model,
            timeout_seconds=config.gemini.timeout_seconds,
            max_retries=config.gemini.max_retries,
        )

    if provider == "openai":
        model = model_override or config.openai.model
        return OpenAIClient(
            model=model,
            timeout_seconds=config.openai.timeout_seconds,
            max_retries=config.openai.max_retries,
        )

    if provider == "claude":
        model = model_override or config.claude.model
        return ClaudeClient(
            model=model,
            timeout_seconds=config.claude.timeout_seconds,
            max_retries=config.claude.max_retries,
            max_tokens=config.claude.max_tokens,
        )

    if provider == "llamacpp":
        # model 未指定時はロール別デフォルトはなく、明示指定を要求
        if not model_override:
            raise ValueError(
                f"llm.{role}.model must be set when provider='llamacpp' "
                "(matches a model name in llama-swap config.yaml)"
            )
        return LlamaCppClient(
            base_url=config.llm.llamacpp.base_url,
            model=model_override,
            timeout_seconds=config.llm.llamacpp.timeout_seconds,
            max_retries=config.llm.llamacpp.max_retries,
        )

    if provider == "claude-cli":
        if not model_override:
            raise ValueError(
                f"llm.{role}.model must be set when provider='claude-cli' "
                "(e.g. 'claude-haiku-4-5' or 'claude-sonnet-4-6')"
            )
        cfg = config.llm.claude_cli
        return ClaudeCliClient(
            model=model_override,
            command=cfg.command,
            isolated_cwd=cfg.isolated_cwd or None,
            timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            extra_args=list(cfg.extra_args),
        )

    # デフォルト: ollama
    model = model_override or _DEFAULT_OLLAMA_MODEL
    return OllamaClient(
        base_url=config.llm.ollama.base_url,
        model=model,
        timeout_seconds=config.llm.ollama.timeout_seconds,
        max_retries=config.llm.ollama.max_retries,
    )
