"""OpenAI-compatible request schema (the subset the gateway understands)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: str | list[dict[str, Any]] | None = None

    def text(self) -> str:
        """Flatten string or content-part content into plain text."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "".join(
            part.get("text", "") for part in self.content if part.get("type") == "text"
        )


class ChatCompletionRequest(BaseModel):
    # Unknown OpenAI fields (tools, response_format, ...) pass through to openai_compat upstreams.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
