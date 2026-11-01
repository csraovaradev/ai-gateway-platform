"""Provider for any OpenAI-compatible upstream: hosted APIs, vLLM, Ollama, LiteLLM, etc."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx

from ..config import ModelSpec
from ..schemas import ChatCompletionRequest
from ..vault import LeasedSecret, VaultError
from .base import ProviderError


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(
        self,
        spec: ModelSpec,
        client: httpx.AsyncClient | None = None,
        api_key: LeasedSecret | None = None,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.url = f"{(spec.base_url or '').rstrip('/')}/chat/completions"
        self.upstream_model = spec.upstream_model or spec.name
        self._client = client or httpx.AsyncClient(timeout=spec.timeout_seconds)

    async def _headers(self) -> dict[str, str]:
        key = None
        if self._api_key is not None:
            try:
                key = (await anyio.to_thread.run_sync(self._api_key.get)).get("api_key")
            except VaultError as exc:
                raise ProviderError(str(exc)) from exc
        elif self.spec.api_key_env:
            key = os.environ.get(self.spec.api_key_env)
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _body(self, req: ChatCompletionRequest, stream: bool) -> dict[str, Any]:
        body = req.model_dump(exclude_none=True)
        body["model"] = self.upstream_model
        body["stream"] = stream
        return body

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> dict[str, Any]:
        headers = await self._headers()
        try:
            resp = await self._client.post(self.url, json=self._body(req, False), headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"upstream unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"upstream returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        data["id"] = request_id
        data["model"] = req.model  # report the gateway's model name, not the upstream id
        return data

    async def stream(
        self, req: ChatCompletionRequest, request_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        headers = await self._headers()
        try:
            async with self._client.stream(
                "POST", self.url, json=self._body(req, True), headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise ProviderError(f"upstream returned {resp.status_code}: {body[:200]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    event["id"] = request_id
                    event["model"] = req.model
                    yield event
        except httpx.HTTPError as exc:
            raise ProviderError(f"upstream unreachable: {exc.__class__.__name__}") from exc
