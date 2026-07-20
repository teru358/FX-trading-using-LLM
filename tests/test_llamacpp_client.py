"""LlamaCppClient と make_embed_fn の単体テスト。

httpx.AsyncClient をモックし、OpenAI 互換リクエスト/レスポンスの組み立てを検証する。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.llm.factory import create_llm_client
from src.llm.llamacpp_client import LlamaCppClient
from src.rag.embedder import embed_text, make_embed_fn


@pytest.fixture
def fake_httpx_response():
    """OpenAI 互換レスポンスを返す httpx.Response モック。"""

    def _build(json_payload):
        resp = MagicMock(spec=httpx.Response)
        resp.json = MagicMock(return_value=json_payload)
        resp.raise_for_status = MagicMock()
        resp.is_error = False  # 正常系: エラー本文ログ分岐に入らない
        return resp

    return _build


def _mock_async_client(response):
    """AsyncClient の async context manager を生成する helper。"""
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client)
    async_ctx.__aexit__ = AsyncMock(return_value=None)
    return async_ctx, client


@pytest.mark.asyncio
async def test_llamacpp_client_chat_completion(fake_httpx_response):
    """chat() が OpenAI 互換ペイロードで POST し、content を抽出する。"""
    resp = fake_httpx_response({
        "choices": [{"message": {"content": "  Hello world  "}}],
    })
    async_ctx, mock_client = _mock_async_client(resp)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        cli = LlamaCppClient(
            base_url="http://localhost:8080/v1",
            model="llama3.1-8b",
        )
        out = await cli.chat(
            [{"role": "user", "content": "hi"}], temperature=0.2,
        )

    assert out == "Hello world"  # trim 済み
    # POST URL は /chat/completions に解決される
    called_url = mock_client.post.call_args[0][0]
    assert called_url == "http://localhost:8080/v1/chat/completions"
    body = mock_client.post.call_args.kwargs["json"]
    assert body["model"] == "llama3.1-8b"
    assert body["temperature"] == 0.2
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_llamacpp_client_folds_system_into_user(fake_httpx_response):
    """Gemma 対策: 先頭 system を最初の user に畳み込んで POST する。"""
    resp = fake_httpx_response({
        "choices": [{"message": {"content": "ok"}}],
    })
    async_ctx, mock_client = _mock_async_client(resp)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        cli = LlamaCppClient(base_url="http://x/v1", model="gemma3-4b")
        await cli.chat([
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
        ])

    body = mock_client.post.call_args.kwargs["json"]
    # system ロールは送らない (Gemma テンプレートが 400 を返すため)
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"] == [{"role": "user", "content": "SYS\n\nhi"}]


def test_fold_system_into_user_no_system_is_noop():
    """system が無ければそのまま返す。"""
    msgs = [{"role": "user", "content": "hi"}]
    assert LlamaCppClient._fold_system_into_user(msgs) == msgs


def test_fold_system_into_user_with_trailing_turns():
    """リトライで assistant/user が追加された会話でも system のみ畳み込む。"""
    folded = LlamaCppClient._fold_system_into_user([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ])
    assert folded == [
        {"role": "user", "content": "SYS\n\nu1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]


def test_fold_system_into_user_system_only():
    """user が無く system だけの場合は user に変換する。"""
    folded = LlamaCppClient._fold_system_into_user([
        {"role": "system", "content": "SYS"},
    ])
    assert folded == [{"role": "user", "content": "SYS"}]


@pytest.mark.asyncio
async def test_llamacpp_client_strips_end_tokens(fake_httpx_response):
    """モデルが <|eot_id|> などを返したら切り捨てる。"""
    resp = fake_httpx_response({
        "choices": [{"message": {"content": "hello<|eot_id|>extra"}}],
    })
    async_ctx, _ = _mock_async_client(resp)
    with patch("httpx.AsyncClient", return_value=async_ctx):
        cli = LlamaCppClient(base_url="http://x/v1", model="m")
        out = await cli.chat([{"role": "user", "content": "hi"}])
    assert out == "hello"


def _make_llm_config(provider: str, *, model: str, base_url: str = "", **pc_overrides):
    """新スキーマ準拠のテスト用 config を組み立てる。"""
    pc_kwargs = dict(
        base_url=base_url,
        command="",
        isolated_cwd="",
        extra_args=[],
        max_tokens=0,
        timeout_seconds=300,
        max_retries=2,
        max_concurrent=2,
    )
    pc_kwargs.update(pc_overrides)
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=provider,
            provider_config=SimpleNamespace(**pc_kwargs),
            news_analysis=SimpleNamespace(model=model, temperature=0.3),
            price_analysis=SimpleNamespace(model=model, temperature=0.1),
            reflection=SimpleNamespace(model=model, temperature=0.3),
        ),
    )


def test_factory_creates_llamacpp_client():
    """provider=llamacpp + model 指定で LlamaCppClient を返す。"""
    config = _make_llm_config(
        "llamacpp",
        model="llama3.1-8b",
        base_url="http://localhost:8080/v1",
    )
    client = create_llm_client(config, "news_analysis")
    assert isinstance(client, LlamaCppClient)
    assert client.model_name == "llama3.1-8b"
    assert client.provider_name == "llamacpp"


@pytest.mark.asyncio
async def test_embed_text_llamacpp_uses_openai_format(fake_httpx_response):
    """provider=llamacpp で /v1/embeddings を OpenAI 形式で叩く。"""
    resp = fake_httpx_response({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    async_ctx, mock_client = _mock_async_client(resp)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        vec = await embed_text(
            text="hello",
            provider="llamacpp",
            base_url="http://localhost:8080/v1",
            model="nomic-embed-text",
        )

    assert vec == [0.1, 0.2, 0.3]
    called_url = mock_client.post.call_args[0][0]
    assert called_url == "http://localhost:8080/v1/embeddings"
    body = mock_client.post.call_args.kwargs["json"]
    assert body["input"] == "hello"
    assert body["model"] == "nomic-embed-text"
    # Ollama 形式の "prompt" キーを使っていないことを確認
    assert "prompt" not in body


@pytest.mark.asyncio
async def test_embed_text_ollama_uses_legacy_format(fake_httpx_response):
    """デフォルト provider=ollama では /api/embeddings を使う。"""
    resp = fake_httpx_response({"embedding": [0.5, 0.6]})
    async_ctx, mock_client = _mock_async_client(resp)

    with patch("httpx.AsyncClient", return_value=async_ctx):
        vec = await embed_text(
            text="hi",
            ollama_base_url="http://localhost:11434",
            model="nomic-embed-text",
        )

    assert vec == [0.5, 0.6]
    called_url = mock_client.post.call_args[0][0]
    assert called_url == "http://localhost:11434/api/embeddings"
    body = mock_client.post.call_args.kwargs["json"]
    assert body["prompt"] == "hi"


def test_make_embed_fn_ollama_default():
    """config.embedding.provider 未指定時は ollama にフォールバック。

    新スキーマでは embedding は config.embedding に独立した base_url を持つ
    (LLM 用 provider_config.base_url とは分離)。
    """
    config = SimpleNamespace(
        embedding=SimpleNamespace(
            model="nomic-embed-text",
            base_url="http://localhost:11434",
        ),
    )
    fn = make_embed_fn(config)
    # functools.partial 経由で keyword args を検証
    assert fn.keywords["provider"] == "ollama"
    assert fn.keywords["ollama_base_url"] == "http://localhost:11434"


def test_make_embed_fn_llamacpp():
    """embedding.provider=llamacpp で base_url が llama-swap に切替わる。"""
    config = SimpleNamespace(
        embedding=SimpleNamespace(
            model="nomic-embed-text",
            provider="llamacpp",
            base_url="http://localhost:8080/v1",
        ),
    )
    fn = make_embed_fn(config)
    assert fn.keywords["provider"] == "llamacpp"
    assert fn.keywords["base_url"] == "http://localhost:8080/v1"
