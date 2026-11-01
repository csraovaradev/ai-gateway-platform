"""Mock provider: echoes the last user message. Lets the gateway run with no cloud credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..config import ModelSpec
from ..schemas import ChatCompletionRequest
from .base import chunk, completion


class MockProvider:
    kind = "mock"

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    @staticmethod
    def _reply(req: ChatCompletionRequest) -> tuple[str, int]:
        user_msgs = [m for m in req.messages if m.role == "user"]
        last = user_msgs[-1].text() if user_msgs else ""
        prompt_tokens = sum(len(m.text().split()) for m in req.messages)
        return f"echo: {last}", prompt_tokens

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> dict[str, Any]:
        text, prompt_tokens = self._reply(req)
        return completion(request_id, req.model, text, prompt_tokens, len(text.split()))

    async def stream(
        self, req: ChatCompletionRequest, request_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        text, _ = self._reply(req)
        yield chunk(request_id, req.model, {"role": "assistant", "content": ""})
        words = text.split(" ")
        for i, word in enumerate(words):
            yield chunk(request_id, req.model, {"content": word if i == 0 else " " + word})
        yield chunk(request_id, req.model, {}, finish_reason="stop")
