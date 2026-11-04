import asyncio

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestServer

from maubot_llm.backends import (
    AnthropicBackend,
    BackendError,
    BasicOpenAIBackend,
    OpenAIBackend,
)


@pytest_asyncio.fixture
async def mock_server():
    app = web.Application()

    async def chat(request):
        body = await request.json()
        if request.headers.get("Authorization") != "Bearer local-test-key":
            return web.json_response({"error": "unauthorized"}, status=401)
        if body.get("model") == "missing":
            return web.json_response({"error": "model not found"}, status=404)
        if body.get("model") == "forbidden":
            return web.json_response({"error": "forbidden"}, status=403)
        if body.get("model") == "rate-limited":
            return web.json_response({"error": "busy"}, status=429)
        if body.get("model") == "unavailable":
            return web.json_response({"error": "failed"}, status=503)
        if body.get("model") == "malformed":
            return web.json_response({"choices": []})
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "model": body.get("model", "mock"),
            }
        )

    async def models(_request):
        return web.json_response({"data": [{"id": "mock-model"}]})

    async def slow(_request):
        await asyncio.sleep(0.1)
        return web.json_response({"choices": []})

    async def malformed(_request):
        return web.json_response({"choices": []})

    async def invalid_json(_request):
        return web.Response(text="not-json")

    async def anthropic(request):
        assert request.headers["x-api-key"] == "anthropic-test-key"
        body = await request.json()
        assert body["system"] == "be helpful"
        return web.json_response(
            {
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "model": "claude-mock",
            }
        )

    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/slow", slow)
    app.router.add_post("/malformed", malformed)
    app.router.add_post("/invalid-json", invalid_json)
    app.router.add_post("/v1/messages", anthropic)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_openai_compatible_chat_and_model_discovery(mock_server):
    backend = BasicOpenAIBackend(
        {
            "base_url": str(mock_server.make_url("/")).rstrip("/"),
            "api_key": "local-test-key",
            "default_model": "mock-model",
        }
    )
    async with aiohttp.ClientSession() as http:
        completion = await backend.create_chat_completion(
            http,
            [{"role": "user", "content": "hello"}],
            system="be concise",
            model="mock-model",
        )
        assert completion.message == {"role": "assistant", "content": "hello"}
        assert completion.finish_reason == "stop"
        assert completion.model == "mock-model"
        assert await backend.fetch_models(http) == ["mock-model"]


@pytest.mark.asyncio
async def test_openai_backend_uses_bearer_auth(mock_server):
    backend = OpenAIBackend(
        {
            "base_url": str(mock_server.make_url("/")).rstrip("/"),
            "api_key": "local-test-key",
        }
    )
    async with aiohttp.ClientSession() as http:
        result = await backend.create_chat_completion(
            http, [{"role": "user", "content": "test"}]
        )
    assert result.message["content"] == "hello"


@pytest.mark.asyncio
async def test_http_errors_and_malformed_responses(mock_server):
    base_url = str(mock_server.make_url("/")).rstrip("/")
    backend = BasicOpenAIBackend({"base_url": base_url, "api_key": "wrong"})
    async with aiohttp.ClientSession() as http:
        with pytest.raises(BackendError, match="rejected authentication"):
            await backend.create_chat_completion(
                http, [{"role": "user", "content": "test"}]
            )

        backend.headers["Authorization"] = "Bearer local-test-key"
        with pytest.raises(BackendError, match="not found"):
            await backend.create_chat_completion(
                http, [{"role": "user", "content": "test"}], model="missing"
            )
        for model, error in (
            ("forbidden", "rejected authentication"),
            ("rate-limited", "rate limiting"),
            ("unavailable", "temporarily unavailable"),
        ):
            with pytest.raises(BackendError, match=error):
                await backend.create_chat_completion(
                    http, [{"role": "user", "content": "test"}], model=model
                )

        with pytest.raises(BackendError, match="no usable assistant"):
            await backend.create_chat_completion(
                http, [{"role": "user", "content": "test"}], model="malformed"
            )
        with pytest.raises(BackendError, match="invalid JSON"):
            await backend._request_json(http, "POST", f"{base_url}/invalid-json")


@pytest.mark.asyncio
async def test_timeout_and_url_validation(mock_server):
    backend = BasicOpenAIBackend(
        {
            "base_url": str(mock_server.make_url("/")).rstrip("/"),
            "api_key": "local-test-key",
        }
    )
    backend.timeout = ClientTimeout(total=0.01)
    async with aiohttp.ClientSession() as http:
        with pytest.raises(BackendError, match="timed out"):
            await backend._request_json(http, "POST", f"{backend.base_url}/slow")
    with pytest.raises(ValueError, match="absolute HTTP"):
        BasicOpenAIBackend({"base_url": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="without credentials"):
        BasicOpenAIBackend({"base_url": "https://user:pass@example.org"})


@pytest.mark.asyncio
async def test_anthropic_adapter_preserves_compatibility(mock_server):
    backend = AnthropicBackend(
        {
            "base_url": str(mock_server.make_url("/")).rstrip("/"),
            "api_key": "anthropic-test-key",
            "max_tokens": 64,
        }
    )
    assert backend.max_tokens == 64
    async with aiohttp.ClientSession() as http:
        result = await backend.create_chat_completion(
            http,
            [{"role": "user", "content": "hello"}],
            system="be helpful",
            model="claude-mock",
        )
    assert result.message == {"role": "assistant", "content": "hello"}
    assert result.finish_reason == "end_turn"
    with pytest.raises(BackendError, match="Model discovery"):
        await backend.fetch_models(None)
