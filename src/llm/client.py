from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from src.llm.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# プロバイダー別の共有サーキットブレーカー
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(provider: str) -> CircuitBreaker:
    """プロバイダー名でサーキットブレーカーを取得（シングルトン）。"""
    if provider not in _circuit_breakers:
        _circuit_breakers[provider] = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=300,
            name=provider,
        )
    return _circuit_breakers[provider]


class CircuitOpenError(Exception):
    """サーキットブレーカーが OPEN 状態で呼び出しがスキップされた場合の例外。"""


class LLMClient(ABC):
    """LLM呼び出し抽象インターフェース。Ollama / Gemini等のプロバイダーを統一的に扱う。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """使用中のモデル名を返す（例: "llama3.1:8b", "gemini-2.0-flash"）。"""

    @property
    def provider_name(self) -> str:
        """サーキットブレーカー用のプロバイダー名。サブクラスでオーバーライド可。"""
        return self.__class__.__name__

    @abstractmethod
    async def _do_chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
    ) -> str:
        """実際の LLM 呼び出し。サブクラスが実装する。"""

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
    ) -> str:
        """チャット形式でLLMを呼び出し、応答テキストを返す。

        サーキットブレーカーが OPEN の場合は CircuitOpenError を投げる。
        """
        cb = get_circuit_breaker(self.provider_name)
        if not cb.allow_request():
            raise CircuitOpenError(
                f"{self.provider_name} circuit is OPEN — skipping LLM call"
            )
        try:
            result = await self._do_chat(messages, temperature)
            cb.record_success()
            return result
        except CircuitOpenError:
            raise
        except Exception as e:
            # usage limit 系エラーは別カウンタで集計 (endpoint で可視化するため)
            # claude_cli_client を遅延 import (循環依存回避)
            is_usage_limit = False
            try:
                from src.llm.claude_cli_client import ClaudeCliUsageLimitError
                is_usage_limit = isinstance(e, ClaudeCliUsageLimitError)
            except ImportError:
                pass
            cb.record_failure(
                error_type=type(e).__name__,
                message=str(e),
                is_usage_limit=is_usage_limit,
            )
            raise


def require_env(var_name: str, provider: str) -> str:
    """環境変数を必須として読む。未設定時は EnvironmentError を投げる。"""
    value = os.environ.get(var_name, "")
    if not value:
        raise EnvironmentError(
            f"{var_name} を .env に設定してください。 (provider={provider})"
        )
    return value
