"""ClaudeCliClient のユニットテスト。

実際の `claude` プロセスは spawn せず、asyncio subprocess をモックする。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.claude_cli_client import (
    ClaudeCliClient,
    ClaudeCliError,
    ClaudeCliTimeoutError,
    ClaudeCliUsageLimitError,
    _format_messages_as_prompt,
)
from src.llm.client import _circuit_breakers
from src.llm.factory import create_llm_client


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """テスト毎にサーキットブレーカー状態をリセット (プロバイダー間の失敗累積を遮断)。"""
    _circuit_breakers.clear()
    yield
    _circuit_breakers.clear()


# ── helper: asyncio subprocess モック ──────────────────────────────────────────


def _mock_proc(stdout: str, stderr: str = "", returncode: int = 0):
    """asyncio.subprocess.Process モック (communicate が指定バイトを返す)。"""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ── _format_messages_as_prompt ──────────────────────────────────────────────────


def test_format_messages_as_prompt_system_and_user():
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Say hi."},
    ]
    prompt = _format_messages_as_prompt(messages)
    assert "[System instructions]" in prompt
    assert "Be concise." in prompt
    assert "[User]" in prompt
    assert "Say hi." in prompt
    assert prompt.rstrip().endswith("[Assistant response - output only the requested content, no preamble]")


def test_format_messages_as_prompt_assistant_role_included():
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    prompt = _format_messages_as_prompt(messages)
    assert "[Previous assistant response]" in prompt
    assert "a1" in prompt


# ── ClaudeCliClient._spawn / _do_chat ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_success_extracts_result_field():
    """正常系: json output の result フィールドを返す。"""
    stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "  Hello  ",
        "cost_usd": 0.01,
    })
    client = ClaudeCliClient(model="claude-haiku-4-5")

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=_mock_proc(stdout)),
    ):
        resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp == "Hello"  # strip 済み


@pytest.mark.asyncio
async def test_chat_strips_end_tokens():
    """<|eot_id|> を切り捨てる。"""
    stdout = json.dumps({
        "type": "result", "subtype": "success",
        "result": "answer<|eot_id|>trailing junk",
    })
    client = ClaudeCliClient(model="claude-haiku-4-5")
    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=_mock_proc(stdout)),
    ):
        resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp == "answer"


@pytest.mark.asyncio
async def test_chat_nonzero_exit_raises_cli_error():
    """非ゼロ exit + usage limit 無関係な文言 → ClaudeCliError。"""
    client = ClaudeCliClient(model="claude-haiku-4-5", max_retries=1)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_mock_proc("", "auth error", returncode=2)),
    ):
        with pytest.raises(ClaudeCliError):
            await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_usage_limit_from_stderr():
    """stderr に usage limit 文言 → ClaudeCliUsageLimitError (retry しない)。"""
    client = ClaudeCliClient(model="claude-haiku-4-5", max_retries=3)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(
            stdout="", stderr="you have hit the usage limit", returncode=1,
        )),
    ):
        with pytest.raises(ClaudeCliUsageLimitError):
            await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_usage_limit_from_stdout_success_exit():
    """exit=0 でも stdout に usage limit 文言 → UsageLimitError。"""
    client = ClaudeCliClient(model="claude-haiku-4-5", max_retries=1)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(
            stdout="Please upgrade your plan to continue.",
        )),
    ):
        with pytest.raises(ClaudeCliUsageLimitError):
            await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_session_limit_429_raises_usage_limit_error():
    """実際の 429 session limit メッセージ → ClaudeCliUsageLimitError。

    本番 (2026-06-26 01:37) で claude-cli が返した実メッセージ。
    api_error_status=429 + "session limit" を usage limit として分類し、
    基底 ClaudeCliError ではなく UsageLimitError に倒す (retry しない / 上位で fail-safe)。
    """
    client = ClaudeCliClient(model="claude-sonnet-4-6", max_retries=3)
    body = (
        '{"type":"result","subtype":"success","is_error":true,'
        '"api_error_status":429,"result":'
        '"You\'ve hit your session limit · resets 1:40am (Asia/Tokyo)"}'
    )
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(stdout=body, stderr="", returncode=1)),
    ):
        with pytest.raises(ClaudeCliUsageLimitError):
            await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_malformed_json_raises():
    client = ClaudeCliClient(model="claude-haiku-4-5", max_retries=1)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_mock_proc(stdout="not a json")),
    ):
        with pytest.raises(ClaudeCliError):
            await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_chat_timeout_kills_process():
    """wait_for が TimeoutError を投げたら kill + ClaudeCliTimeoutError。"""
    import asyncio as _asyncio

    client = ClaudeCliClient(model="claude-haiku-4-5", timeout_seconds=1, max_retries=1)

    proc = MagicMock()
    proc.communicate = AsyncMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    async def _raise_timeout(*args, **kwargs):
        raise _asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("asyncio.wait_for", side_effect=_raise_timeout):
        with pytest.raises(ClaudeCliTimeoutError):
            await client.chat([{"role": "user", "content": "hi"}])
    proc.kill.assert_called_once()


# ── factory 統合 ────────────────────────────────────────────────────────────────


def test_factory_creates_claude_cli_client():
    """新スキーマ: provider=claude-cli + provider_config + 役割 model で生成。

    旧 'model 未指定で factory が ValueError' は loader 側のバリデーションに
    移管されたため、本ファイルからは削除 (test_loader_llm_config.py で検証)。
    """
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="claude-cli",
            provider_config=SimpleNamespace(
                base_url="",
                command="claude",
                isolated_cwd="/opt/finance-cli",
                extra_args=["--flag-a"],
                max_tokens=0,
                timeout_seconds=90,
                max_retries=2,
                max_concurrent=2,
            ),
            news_analysis=SimpleNamespace(model="claude-haiku-4-5", temperature=0.3),
            price_analysis=SimpleNamespace(model="claude-sonnet-4-6", temperature=0.1),
            reflection=SimpleNamespace(model="claude-haiku-4-5", temperature=0.3),
        ),
    )
    client = create_llm_client(config, "news_analysis")
    assert isinstance(client, ClaudeCliClient)
    assert client.model_name == "claude-haiku-4-5"
    assert client.provider_name == "claude-cli"


# ── argv 組み立て ────────────────────────────────────────────────────────────────


def test_build_argv_includes_model_output_format_and_extra_args():
    client = ClaudeCliClient(
        model="claude-sonnet-4-6",
        command="/usr/local/bin/claude",
        extra_args=["--dangerously-skip-permissions"],
    )
    argv = client._build_argv("test prompt")
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert "--model" in argv
    assert "claude-sonnet-4-6" in argv
    assert "--output-format" in argv
    assert "json" in argv
    assert "--dangerously-skip-permissions" in argv
    # プロンプトは最後
    assert argv[-1] == "test prompt"


# ── subprocess env clean-up ────────────────────────────────────────────────────


def test_child_env_strips_anthropic_api_keys(monkeypatch):
    """外部 API キー系の env が subprocess から除外される。

    Claude CLI は ANTHROPIC_API_KEY 等が存在するとサブスク認証を使わず
    "Invalid API key" エラーになるため、明示的にクリアする必要がある。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-yyy")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    monkeypatch.setenv("CLAUDE_API_KEY", "key-zzz")
    monkeypatch.setenv("OTHER_VAR", "keep-me")

    client = ClaudeCliClient(model="claude-haiku-4-5")
    env = client._child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDE_API_KEY" not in env
    # 他の変数は保持される (PATH など)
    assert env.get("OTHER_VAR") == "keep-me"
