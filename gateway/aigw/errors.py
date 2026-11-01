"""Errors returned to clients in OpenAI format: {"error": {"message", "type"}}."""

from __future__ import annotations


class GatewayError(Exception):
    def __init__(self, status: int, message: str, kind: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.kind, self.headers = status, message, kind, headers
