"""Amazon Bedrock provider using the Converse API.

With `vault_path` set, credentials are short-lived STS credentials leased from Vault's AWS
secrets engine and the boto3 client is rebuilt whenever they rotate (ADR-0001). Without it,
the default AWS credential chain is used (env vars, profile, or IRSA on EKS).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
from starlette.concurrency import iterate_in_threadpool

from ..config import ModelSpec
from ..schemas import ChatCompletionRequest
from ..vault import LeasedSecret
from .base import ProviderError, chunk, completion, scrub, usage

_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
_ANTHROPIC_VERSION = "bedrock-2023-05-31"
# Cross-region inference profiles ("us.anthropic...") prefix the base model ID with a geography.
_PROFILE_PREFIX = re.compile(r"^[a-z-]+\.(?=anthropic\.)")
_EXPIRED = ("ExpiredTokenException", "ExpiredToken")


def _invoke_error(exc: Exception) -> ProviderError:
    """Map a Bedrock InvokeModel failure to what the client may see (ADR-0006)."""
    if isinstance(exc, ProviderError):
        return exc
    error = getattr(exc, "response", {}).get("Error", {})
    code, message = error.get("Code", ""), error.get("Message", str(exc))
    code = code[:1].upper() + code[1:]  # stream errors arrive as "validationException"
    detail = f"bedrock {code or type(exc).__name__}: {message}"
    if code == "ValidationException":
        # Request-shape errors: Claude Code matches this wording to retry without a capability.
        return ProviderError(detail, status=400, client_message=scrub(message))
    if code in ("ThrottlingException", "ServiceQuotaExceededException"):
        return ProviderError(detail, status=429, retry_after=5,
                             client_message="upstream is throttling requests; retry shortly")
    if code in ("ModelNotReadyException", "ServiceUnavailableException"):
        return ProviderError(detail, status=503, retry_after=5)
    return ProviderError(detail)


def _invoke_body(body: dict[str, Any], betas: list[str]) -> bytes:
    """Anthropic Messages body in Bedrock's dialect: model goes in the URL, betas in the body."""
    payload = {k: v for k, v in body.items() if k not in ("model", "stream")}
    payload["anthropic_version"] = _ANTHROPIC_VERSION
    if betas:
        payload["anthropic_beta"] = betas
    return json.dumps(payload).encode()


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
            from botocore.config import Config

            # boto3's defaults allow several attempts of 60 s each; bound the total wait instead.
            config = Config(connect_timeout=5, read_timeout=self.spec.timeout_seconds,
                            retries={"total_max_attempts": 2, "mode": "standard"})
            self._client = boto3.client("bedrock-runtime", region_name=self.spec.region,
                                        config=config, **keys)
            self._client_creds = creds
        return self._client

    async def _call(self, operation: Callable[[Any], Any]) -> Any:
        """Run a boto3 call in a thread. If AWS says the leased credentials expired, fetch new
        ones from Vault and try once more, so a stale cache never reaches the client."""
        try:
            return await anyio.to_thread.run_sync(lambda: operation(self._bedrock()))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if self._credentials is None or code not in _EXPIRED:
                raise
            self._credentials.invalidate()
        return await anyio.to_thread.run_sync(lambda: operation(self._bedrock()))

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
            resp = await self._call(lambda bedrock: bedrock.converse(**body))
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
            resp = await self._call(lambda bedrock: bedrock.converse_stream(**body))
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"bedrock error: {exc}") from exc
        yield chunk(request_id, req.model, {"role": "assistant", "content": ""})
        reason, tokens = "stop", None
        async for event in iterate_in_threadpool(iter(resp["stream"])):
            if "contentBlockDelta" in event:
                text = event["contentBlockDelta"].get("delta", {}).get("text")
                if text:
                    yield chunk(request_id, req.model, {"content": text})
            elif "messageStop" in event:
                reason = _STOP_REASONS.get(event["messageStop"].get("stopReason", ""), "stop")
            elif "metadata" in event:  # arrives after messageStop and carries token usage
                u = event["metadata"].get("usage", {})
                tokens = usage(u.get("inputTokens", 0), u.get("outputTokens", 0))
        yield chunk(request_id, req.model, {}, finish_reason=reason, usage=tokens)

    # Anthropic Messages API (ADR-0006): the body passes through in Anthropic's own format.

    async def messages(self, body: dict[str, Any], betas: list[str]) -> dict[str, Any]:
        payload = _invoke_body(body, betas)
        try:
            resp = await self._call(lambda bedrock: bedrock.invoke_model(
                modelId=self.spec.upstream_model, body=payload,
                contentType="application/json", accept="application/json"))
            return json.loads(resp["body"].read())
        except Exception as exc:
            raise _invoke_error(exc) from exc

    async def messages_stream(
        self, body: dict[str, Any], betas: list[str]
    ) -> AsyncIterator[dict[str, Any]]:
        payload = _invoke_body(body, betas)
        try:
            resp = await self._call(lambda bedrock: bedrock.invoke_model_with_response_stream(
                modelId=self.spec.upstream_model, body=payload,
                contentType="application/json", accept="application/json"))
        except Exception as exc:
            raise _invoke_error(exc) from exc
        try:
            async for part in iterate_in_threadpool(iter(resp["body"])):
                if "chunk" not in part:
                    continue
                event = json.loads(part["chunk"]["bytes"])
                event.pop("amazon-bedrock-invocationMetrics", None)
                yield event
        except Exception as exc:
            raise _invoke_error(exc) from exc

    async def count_tokens(self, body: dict[str, Any], betas: list[str]) -> int:
        # CountTokens validates the InvokeModel body, which requires max_tokens, and accepts only
        # base model IDs, not the cross-region inference profiles used for inference.
        payload = _invoke_body({"max_tokens": 1, **body}, betas)
        base_model = _PROFILE_PREFIX.sub("", self.spec.upstream_model or "")
        try:
            resp = await self._call(lambda bedrock: bedrock.count_tokens(
                modelId=base_model, input={"invokeModel": {"body": payload}}))
        except Exception as exc:
            raise _invoke_error(exc) from exc
        return int(resp["inputTokens"])

