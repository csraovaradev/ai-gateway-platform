"""Amazon Bedrock provider using the Converse API.

With `vault_path` set, credentials are short-lived STS credentials leased from Vault's AWS
secrets engine and the boto3 client is rebuilt whenever they rotate (ADR-0001). Without it,
the default AWS credential chain is used (env vars, profile, or IRSA on EKS).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
from starlette.concurrency import iterate_in_threadpool

from ..config import ModelSpec
from ..schemas import ChatCompletionRequest
from ..vault import LeasedSecret
from .base import ProviderError, chunk, completion

_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class BedrockProvider:
    kind = "bedrock"

    def __init__(self, spec: ModelSpec, credentials: LeasedSecret | None = None) -> None:
        self.spec = spec
        self._credentials = credentials
        self._client: Any = None
        self._client_creds: dict[str, Any] | None = None

    def _bedrock(self) -> Any:
        creds = self._credentials.get() if self._credentials else None
        if self._client is None or creds is not self._client_creds:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover
                raise ProviderError("boto3 not installed; install aigw[bedrock]") from exc
            keys: dict[str, Any] = {}
            if creds is not None:
                keys = {
                    "aws_access_key_id": creds["access_key"],
                    "aws_secret_access_key": creds["secret_key"],
                    "aws_session_token": creds.get("security_token"),
                }
            self._client = boto3.client("bedrock-runtime", region_name=self.spec.region, **keys)
            self._client_creds = creds
        return self._client

    def _request(self, req: ChatCompletionRequest) -> dict[str, Any]:
        system = [{"text": m.text()} for m in req.messages if m.role in ("system", "developer")]
        messages = [
            {"role": m.role, "content": [{"text": m.text()}]}
            for m in req.messages
            if m.role in ("user", "assistant")
        ]
        inference: dict[str, Any] = {}
        if req.max_tokens is not None:
            inference["maxTokens"] = req.max_tokens
        if req.temperature is not None:
            inference["temperature"] = req.temperature
        if req.top_p is not None:
            inference["topP"] = req.top_p
        body: dict[str, Any] = {"modelId": self.spec.upstream_model, "messages": messages}
        if system:
            body["system"] = system
        if inference:
            body["inferenceConfig"] = inference
        return body

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> dict[str, Any]:
        body = self._request(req)
        try:
            resp = await anyio.to_thread.run_sync(lambda: self._bedrock().converse(**body))
        except ProviderError:
            raise
        except Exception as exc:  # botocore raises many error types
            raise ProviderError(f"bedrock error: {exc}") from exc
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        text = "".join(b.get("text", "") for b in blocks)
        usage = resp.get("usage", {})
        return completion(
            request_id,
            req.model,
            text,
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            _STOP_REASONS.get(resp.get("stopReason", ""), "stop"),
        )

    async def stream(
        self, req: ChatCompletionRequest, request_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        body = self._request(req)
        try:
            resp = await anyio.to_thread.run_sync(lambda: self._bedrock().converse_stream(**body))
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"bedrock error: {exc}") from exc
        yield chunk(request_id, req.model, {"role": "assistant", "content": ""})
        async for event in iterate_in_threadpool(iter(resp["stream"])):
            if "contentBlockDelta" in event:
                text = event["contentBlockDelta"].get("delta", {}).get("text")
                if text:
                    yield chunk(request_id, req.model, {"content": text})
            elif "messageStop" in event:
                reason = _STOP_REASONS.get(event["messageStop"].get("stopReason", ""), "stop")
                yield chunk(request_id, req.model, {}, finish_reason=reason)
