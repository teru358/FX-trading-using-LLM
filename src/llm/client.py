from __future__ import annotations

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
