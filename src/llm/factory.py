from __future__ import annotations

from src.config import AppConfig, ConfigError, ProviderConfig
from src.config.schema import AgentLlm
from src.llm.claude_cli_client import ClaudeCliClient
from src.llm.claude_client import ClaudeClient
from src.llm.client import LLMClient
from src.llm.gemini_client import GeminiClient
from src.llm.llamacpp_client import LlamaCppClient
from src.llm.ollama_client import OllamaClient
from src.llm.openai_client import OpenAIClient

# 有効なロール名
LLM_ROLES = ("news_analysis", "price_analysis", "reflection")


def _build_client(provider: str, pc: ProviderConfig, model: str) -> LLMClient:
    """provider + 接続設定 (ProviderConfig) + model から LLMClient を作る。

    create_llm_client (3役割) と create_agent_llm (agent 別) の共通 dispatch。
    temperature は client に渡さない (chat() 呼出時の引数として扱う = 現行踏襲)。
    """
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

    return _build_client(provider, pc, model)


# 5 agent 名。authoritative source は schema.OrchestratorAgentsLlmConfig の field 集合。
# 同名の tuple が src/config/loader.py:_AGENT_LLM_NAMES にもある (層が違う: loader は
# agents.yaml キー検証、ここは runtime dispatch ガード)。agent を増減したら両方+schema を更新。
AGENT_NAMES = ("planner", "news", "technical", "execution_opinion", "context_summary")

_AGENT_FALLBACK_ROLE = {
    "planner": "price_analysis",
    "news": "news_analysis",
    "technical": "price_analysis",
    "execution_opinion": "price_analysis",
    "context_summary": "reflection",
}


def create_agent_llm(config: AppConfig, agent_name: str) -> AgentLlm:
    """agent 専用 LLM ハンドル (client + temperature) を作る。

    config.agent_llms.<agent_name>.provider が指定されていればその provider/model/
    temperature、空欄なら既存役割 (price_analysis 等) の client + 役割 temperature に
    fallback する (後方互換)。
    """
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"unknown agent '{agent_name}', expected {AGENT_NAMES}")

    agent_cfg = getattr(config.agent_llms, agent_name)  # AgentLlmConfig
    if not agent_cfg.provider:
        role = _AGENT_FALLBACK_ROLE[agent_name]
        role_cfg = getattr(config.llm, role)  # LLMRoleConfig (model + temperature)
        return AgentLlm(
            client=create_llm_client(config, role),
            temperature=role_cfg.temperature,
        )

    # provider 別接続設定を解決 (spec High #3: 危険な空 fallback を禁止)。
    # この解決規則は loader._validate_agent_provider_configs (起動時検証) と同じ不変条件:
    # top-level と同一 provider → 単一 provider_config 流用 / 異なる → provider_configs[provider]
    # 必須・無ければ ConfigError。両者がドリフトしないこと。
    if agent_cfg.provider == config.llm.provider:
        pc = config.llm.provider_config           # top-level と同一 provider なら既存設定を流用
    else:
        pc = config.llm.provider_configs.get(agent_cfg.provider)
        if pc is None:
            raise ConfigError(
                f"agent '{agent_name}' provider '{agent_cfg.provider}' is not defined "
                f"in llm.provider_configs. Add a connection config for it "
                f"(implicit fallback to an empty provider_config is forbidden: "
                f"a missing base_url would silently produce a broken client)."
            )
    return AgentLlm(
        client=_build_client(agent_cfg.provider, pc, agent_cfg.model),
        temperature=agent_cfg.temperature,
    )
