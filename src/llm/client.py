from __future__ import annotations

import os
from abc import ABC, abstractmethod


class LLMClient(ABC):
    """LLM呼び出し抽象インターフェース。Ollama / Gemini等のプロバイダーを統一的に扱う。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """使用中のモデル名を返す（例: "llama3.1:8b", "gemini-2.0-flash"）。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
    ) -> str:
        """チャット形式でLLMを呼び出し、応答テキストを返す。

        messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
        """


def require_env(var_name: str, provider: str) -> str:
    """環境変数を必須として読む。未設定時は EnvironmentError を投げる。"""
    value = os.environ.get(var_name, "")
    if not value:
        raise EnvironmentError(
            f"{var_name} を .env に設定してください。 (provider={provider})"
        )
    return value
