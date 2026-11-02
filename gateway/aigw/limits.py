"""Request body size limit, enforced while the body is read (threat model: DoS)."""

from __future__ import annotations

import json
from typing import Any

Scope = dict[str, Any]


class BodySizeLimit:
    """Pure ASGI middleware: 413 for bodies over `max_bytes`, chunked uploads included."""

    def __init__(self, app: Any, max_bytes: int, overrides: dict[str, int] | None = None) -> None:
        self.app, self.default, self.overrides = app, max_bytes, overrides or {}

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = next((v for p, v in self.overrides.items() if path.startswith(p)), self.default)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            await self._reject(send, limit)
            return
        received, exceeded, rejected = 0, False, False
        end_of_body = {"type": "http.request", "body": b"", "more_body": False}

        # Raising from receive() would not work: FastAPI turns any error while reading the body
        # into a 400. Instead, cut the body off and replace whatever response the app sends.
        async def limited_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            if exceeded:
                return end_of_body
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return end_of_body
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal rejected
            if not exceeded:
                await send(message)
            elif not rejected:
                rejected = True
                await self._reject(send, limit)

        await self.app(scope, limited_receive, guarded_send)

    async def _reject(self, send: Any, limit: int) -> None:
        body = json.dumps({"error": {"message": f"request body over {limit} bytes",
                                     "type": "invalid_request_error"}}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
