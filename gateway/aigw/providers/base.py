"""Provider interface and helpers for building OpenAI-shaped responses."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from ..schemas import ChatCompletionRequest


class ProviderError(Exception):
    """An upstream provider failed. Surfaced to clients as 502."""


class Provider(Protocol):
    kind: str

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> dict[str, Any]: ...

    def stream(self, req: ChatCompletionRequest, request_id: str) -> AsyncIterator[dict[str, Any]]:
        ...


def completion(
    request_id: str,
    model: str,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage(prompt_tokens, completion_tokens),
    }


def usage(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def chunk(
    request_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """A streaming chunk. The final one carries `usage` so streamed requests are metered too."""
    event: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        event["usage"] = usage
    return event
