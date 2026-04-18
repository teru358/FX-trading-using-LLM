from __future__ import annotations

"""Claude Code CLI (`claude -p`) 経由の LLM クライアント。

# 用途
Claude Max プラン等のサブスクリプション枠を使って finance の LLM 呼び出しを処理する。
API キー不要。ローカル LLM GPU 不要 (embed は別途 ollama 等で処理)。

# セットアップ例 (config/settings.yaml)
    llm:
      claude_cli:
        command: "claude"                 # 実体パス (PATH 解決でも可)
        isolated_cwd: "/opt/finance-cli"  # CLAUDE.md 汚染回避用の空ディレクトリ
        timeout_seconds: 120
        max_retries: 2
        extra_args: []                     # 追加フラグ (例: ["--dangerously-skip-permissions"])

      news_analysis:
        provider: "claude-cli"
        model: "claude-haiku-4-5"
        temperature: 0.3                   # CLI では無視されるが残しておく
      price_analysis:
        provider: "claude-cli"
        model: "claude-sonnet-4-6"
      reflection:
        provider: "claude-cli"
        model: "claude-haiku-4-5"

# 前提
- スティック PC / 任意環境に `claude` CLI がインストール済み
- `claude login` で認証済み (~/.claude/ に token)
- isolated_cwd ディレクトリに空または最小限の CLAUDE.md を配置
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from tenacity import Retrying, retry_if_not_exception_type, stop_after_attempt, wait_fixed

from src.llm.client import LLMClient

logger = logging.getLogger(__name__)

# Claude CLI がサブスク認証を使う際に、外部 API キー系 env が存在すると
# 誤ってそちらを使い "Invalid API key" を返すため subprocess から除外する。
_STRIP_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY",
)


class ClaudeCliError(RuntimeError):
    """Claude CLI 呼び出し全般のエラー基底クラス。"""


class ClaudeCliUsageLimitError(ClaudeCliError):
    """usage limit に当たった場合 (retry しても無駄)。"""


class ClaudeCliTimeoutError(ClaudeCliError):
    """タイムアウト (retry 価値あり)。"""


# usage limit を示唆する stderr/stdout パターン
_USAGE_LIMIT_PATTERNS = (
    "usage limit",
    "rate limit",
    "please upgrade",
    "quota exceeded",
    "max messages",
)


def _format_messages_as_prompt(messages: list[dict]) -> str:
    """chat 形式 messages を CLI 用の 1 本のプロンプト文字列に変換。

    system メッセージは先頭に "[System instructions]" として付与、
    user/assistant は role ラベル付きで接続する。
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            parts.append(f"[System instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant response]\n{content}")
        else:  # user
            parts.append(f"[User]\n{content}")
    parts.append("[Assistant response - output only the requested content, no preamble]")
    return "\n\n".join(parts)


class ClaudeCliClient(LLMClient):
    """Claude Code CLI (`claude -p`) subprocess 経由の LLM クライアント。

    OpenAI 互換 API の代わりに CLI プロセスを spawn する。
    サブスクプラン (Max 5x/20x) 内で動作、API キー不要。
    """

    def __init__(
        self,
        model: str,
        command: str = "claude",
        isolated_cwd: str | None = None,
        timeout_seconds: int = 120,
        max_retries: int = 2,
        extra_args: list[str] | None = None,
    ) -> None:
        self._model = model
        self._command = command
        self._isolated_cwd = isolated_cwd
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._extra_args = list(extra_args or [])

        if isolated_cwd and not Path(isolated_cwd).is_dir():
            logger.warning(
                f"[LLM] claude-cli: isolated_cwd {isolated_cwd!r} does not exist; "
                "CLAUDE.md / skills may contaminate responses"
            )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    def _build_argv(self, prompt: str) -> list[str]:
        """実際に起動する argv を組み立てる。"""
        argv = [
            self._command,
            "-p",
            "--model", self._model,
            "--output-format", "json",
        ]
        argv.extend(self._extra_args)
        argv.append(prompt)
        return argv

    def _child_env(self) -> dict[str, str]:
        """subprocess 用の環境変数。CLI がサブスク認証を使うよう
        外部 API キー系の env をクリアする。"""
        env = os.environ.copy()
        for key in _STRIP_ENV_VARS:
            env.pop(key, None)
        return env

    async def _spawn(self, argv: list[str]) -> tuple[int, str, str]:
        """subprocess を spawn し (returncode, stdout, stderr) を返す。"""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._isolated_cwd,
            env=self._child_env(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise ClaudeCliTimeoutError(
                f"claude CLI timed out after {self._timeout}s",
            ) from e
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _detect_usage_limit(text: str) -> bool:
        lower = text.lower()
        return any(p in lower for p in _USAGE_LIMIT_PATTERNS)

    @staticmethod
    def _extract_result_text(stdout: str) -> str:
        """Claude CLI の json 出力から result フィールドを取り出す。

        期待する形式 (非ストリーム json):
          {"type":"result","subtype":"success","result":"<text>", ...}
        """
        try:
            obj = json.loads(stdout.strip())
        except json.JSONDecodeError as e:
            raise ClaudeCliError(
                f"failed to parse claude CLI json: {e}; raw={stdout[:200]!r}",
            ) from e
        if not isinstance(obj, dict):
            raise ClaudeCliError(f"unexpected CLI json shape: {type(obj)}")
        # 成功ケース
        if obj.get("subtype") == "success" and "result" in obj:
            return str(obj["result"])
        # エラーケース (形式はバージョン依存のため柔軟に拾う)
        err = obj.get("error") or obj.get("message") or str(obj)
        raise ClaudeCliError(f"claude CLI error: {err}")

    async def _do_chat(self, messages: list[dict], temperature: float = 0.1) -> str:
        # temperature は CLI では制御できないため無視 (JSON 忠実性は Claude 本体で担保)
        prompt = _format_messages_as_prompt(messages)
        argv = self._build_argv(prompt)

        logger.debug(
            f"[LLM] claude-cli({self._model}): argv={argv[:6]}... prompt_len={len(prompt)}"
        )

        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_fixed(5),
            # usage limit は retry しない、timeout のみ retry
            retry=retry_if_not_exception_type((ClaudeCliUsageLimitError,)),
            reraise=True,
        ):
            with attempt:
                rc, stdout, stderr = await self._spawn(argv)

                if rc != 0:
                    combined = f"{stdout}\n{stderr}"
                    if self._detect_usage_limit(combined):
                        raise ClaudeCliUsageLimitError(
                            f"claude-cli usage limit hit (rc={rc}): {combined[:200]}",
                        )
                    raise ClaudeCliError(
                        f"claude-cli exit={rc}: {combined[:200]}",
                    )

                if self._detect_usage_limit(stdout):
                    raise ClaudeCliUsageLimitError(
                        f"claude-cli usage limit detected in output: {stdout[:200]}",
                    )

                text = self._extract_result_text(stdout)

        # 一部モデルが停止トークン後に自己対話を続けるケースを切り捨て
        for eos in ("<|endoftext|>", "<|im_end|>", "<|eot_id|>"):
            if eos in text:
                text = text[: text.index(eos)]
                break
        return text.strip()
