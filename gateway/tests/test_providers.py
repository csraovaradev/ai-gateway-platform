import asyncio
import json

import httpx
import pytest

from aigw.config import ModelSpec
from aigw.providers import ProviderError
from aigw.providers.bedrock import BedrockProvider
from aigw.providers.openai_compat import OpenAICompatProvider
from aigw.schemas import ChatCompletionRequest

REQ = ChatCompletionRequest.model_validate(
    {"model": "local-llama", "messages": [{"role": "system", "content": "be brief"},
                                           {"role": "user", "content": "hi"}],
     "max_tokens": 50}
)
SPEC = ModelSpec(name="local-llama", provider="openai_compat",
                 base_url="http://upstream/v1", upstream_model="llama3.2")


def run(coro):
    return asyncio.run(coro)


def test_openai_compat_complete_rewrites_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "up-1", "model": "llama3.2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hey"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    provider = OpenAICompatProvider(SPEC, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = run(provider.complete(REQ, "chatcmpl-x"))
    assert seen["body"]["model"] == "llama3.2"  # upstream id sent upstream
    assert result["model"] == "local-llama"  # gateway name returned to client
    assert result["id"] == "chatcmpl-x"


def test_openai_compat_stream():
    sse = (
        'data: {"choices":[{"index":0,"delta":{"content":"he"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"y"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatProvider(SPEC, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def collect():
        return [e async for e in provider.stream(REQ, "chatcmpl-y")]

    events = run(collect())
    assert provider._body(REQ, True)["stream_options"] == {"include_usage": True}
    assert "".join(e["choices"][0]["delta"]["content"] for e in events) == "hey"
    assert all(e["model"] == "local-llama" for e in events)


def test_openai_compat_upstream_error():
    def handler(request):
        return httpx.Response(500, text="kaboom")

    provider = OpenAICompatProvider(SPEC, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError):
        run(provider.complete(REQ, "chatcmpl-z"))


class FakeBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": "hello"}]}},
                "usage": {"inputTokens": 7, "outputTokens": 1}, "stopReason": "max_tokens"}

    def converse_stream(self, **kwargs):
        return {"stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "hel"}}},
            {"contentBlockDelta": {"delta": {"text": "lo"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 2}}},
        ]}


def bedrock_provider():
    provider = BedrockProvider(ModelSpec(name="bedrock-claude", provider="bedrock",
                                         upstream_model="model-id", region="us-west-2"))
    provider._client = FakeBedrock()
    return provider


def test_bedrock_request_mapping_and_complete():
    provider = bedrock_provider()
    result = run(provider.complete(REQ, "chatcmpl-b"))
    call = provider._client.calls[0]
    assert call["system"] == [{"text": "be brief"}]
    assert call["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert call["inferenceConfig"] == {"maxTokens": 50}
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["usage"]["total_tokens"] == 8


def test_bedrock_stream():
    provider = bedrock_provider()

    async def collect():
        return [e async for e in provider.stream(REQ, "chatcmpl-s")]

    events = run(collect())
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert text == "hello"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"] == {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
