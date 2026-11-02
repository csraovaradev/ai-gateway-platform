"""Mock provider: echoes the last user message. Lets the gateway run with no cloud credentials."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..config import ModelSpec
from ..schemas import ChatCompletionRequest
from .base import chunk, completion, usage


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
        text, prompt_tokens = self._reply(req)
        yield chunk(request_id, req.model, {"role": "assistant", "content": ""})
        words = text.split(" ")
        for i, word in enumerate(words):
            yield chunk(request_id, req.model, {"content": word if i == 0 else " " + word})
        yield chunk(request_id, req.model, {}, finish_reason="stop",
                    usage=usage(prompt_tokens, len(text.split())))

    # Anthropic Messages API, echoing the last user text.

    @staticmethod
    def _message_reply(body: dict[str, Any]) -> tuple[str, int]:
        def text(content: Any) -> str:
            if isinstance(content, str):
                return content
            return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")

        users = [m for m in body.get("messages", []) if m.get("role") == "user"]
        last = text(users[-1].get("content")) if users else ""
        tokens = sum(len(text(m.get("content")).split()) for m in body.get("messages", []))
        return f"echo: {last}", tokens

    async def messages(self, body: dict[str, Any], betas: list[str]) -> dict[str, Any]:
        reply, input_tokens = self._message_reply(body)
        return {"id": f"msg_mock_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
                "model": body["model"], "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": len(reply.split())}}

    async def messages_stream(
        self, body: dict[str, Any], betas: list[str]
    ) -> AsyncIterator[dict[str, Any]]:
        reply, input_tokens = self._message_reply(body)
        yield {"type": "message_start", "message": {
            "id": f"msg_mock_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
            "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1}}}
        yield {"type": "content_block_start", "index": 0,
               "content_block": {"type": "text", "text": ""}}
        for i, word in enumerate(reply.split(" ")):
            yield {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": word if i == 0 else " " + word}}
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
               "usage": {"output_tokens": len(reply.split())}}
        yield {"type": "message_stop"}

    async def count_tokens(self, body: dict[str, Any], betas: list[str]) -> int:
        return self._message_reply(body)[1]

