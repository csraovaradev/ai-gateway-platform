"""Provider interface and helpers for building OpenAI-shaped responses."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from ..schemas import ChatCompletionRequest


class ProviderError(Exception):
    """An upstream provider failed.

    `status` is the HTTP status to answer with: 502 unless the upstream rejected the request.
    `client_message` is wording that is safe to show the client; without it clients get a generic
    message and only the audit log sees `str(exc)` (ADR-0005).
    """

    def __init__(self, message: str, status: int = 502, client_message: str | None = None,
                 retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status, self.client_message, self.retry_after = status, client_message, retry_after


_ARN = re.compile(r"arn:aws[\w-]*:[^\s\"',]*")
_ACCOUNT_ID = re.compile(r"\b\d{12}\b")


def scrub(message: str) -> str:
    """Remove ARNs and AWS account IDs from upstream wording before a client sees it."""
    return _ACCOUNT_ID.sub("<account>", _ARN.sub("<arn>", message))


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
