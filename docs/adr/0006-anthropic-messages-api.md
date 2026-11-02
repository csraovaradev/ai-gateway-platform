# ADR-0006: Anthropic Messages API for Claude Code

- **Status:** Accepted
- **Date:** 2026-11-02
- **Follows:** ADR-0001 to ADR-0005

## Context

Developers work in coding agents such as Claude Code. Claude Code speaks the Anthropic Messages API
(`/v1/messages`), not the OpenAI format the gateway served, and its
[gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol.md) is specific:

- forward `anthropic-beta` verbatim and request bodies unchanged (including `system` blocks and
  `cache_control`, or prompt caching silently stops),
- relay streamed events as they arrive, in order, and keep the connection alive during long pauses,
- forward upstream rejection wording, because Claude Code retries without a capability by matching it.

## Decision

1. **`POST /v1/messages`** (and `/v1/messages/count_tokens`, `HEAD /api/hello`) on the same service,
   using the same team keys (`x-api-key` or `Authorization: Bearer`), policy checks (`admit()` is shared
   with `/v1/chat/completions`), rate limits, budgets, and audit.
2. **No translation for Bedrock.** The body goes to Bedrock InvokeModel in Anthropic's own format: the
   model moves to the URL, `anthropic-beta` moves into the body as `anthropic_beta`, and
   `anthropic_version` is set to Bedrock's dialect. Tools, thinking (with signatures), images, and cache
   markers pass through. Converse is still used for the OpenAI-format API.
3. **Gateway model names are Anthropic model IDs** (`claude-sonnet-4-5`) mapped to Bedrock inference
   profiles, so Claude Code recognises the model and sends the request fields that model accepts.
4. **Streaming:** events are relayed one by one; the gateway inserts `ping` events after 10 s of silence,
   because Bedrock's stream has none and Claude Code aborts idle streams. The first event is awaited
   before answering, so an upfront rejection is an HTTP error the client can act on, not a 200 stream.
5. **Errors in Anthropic's envelope** on `/v1/messages*`. Upstream `ValidationException` wording is
   passed through (with ARNs and 12-digit account IDs scrubbed) so Claude Code's capability retries
   work; access, throttling, and server errors stay generic, as in ADR-0005. Budget refusals carry
   `Retry-After: 3600`, which tells Claude Code to stop retrying and show the error.
6. **Metering includes prompt caching:** cache writes and reads are counted and priced (default 1.25x
   and 0.1x the input price, overridable per model). Requests are audited as `chat_completion` with
   `api: messages`, so metrics, SLOs, and the cost report cover both APIs, plus Claude Code's session and
   agent IDs when its client sends them.
7. **Larger body limit for `/v1/messages`** (32 MiB), because an agent resends the whole conversation,
   tool results and images included, on every turn. Other endpoints keep 1 MiB.

## Consequences

- Claude Code runs through the gateway with `ANTHROPIC_BASE_URL` and a gateway key. Verified with the
  Claude Code CLI: tools, streaming, prompt caching, and a session cost that matched Claude Code's own
  figure to the cent.
- Pre-release betas that Bedrock rejects reach the developer as `400`s unless Claude Code can retry
  without them; `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` avoids them for Bedrock-backed gateways.
- Bedrock CountTokens accepts only base model IDs, so the gateway strips the inference-profile prefix.
- Which Claude models work depends on the AWS account's Bedrock access; unavailable ones fail with a
  generic 502 and the AWS reason in the audit log.

## Alternatives considered

- **Translate Messages to Converse.** One upstream API, but lossy: thinking signatures, cache markers,
  and new tool fields would need constant mapping work as Claude Code adds capabilities.
- **Expose Bedrock's own InvokeModel format** (`ANTHROPIC_BEDROCK_BASE_URL`). Simpler for the gateway,
  but ties every developer's configuration to Bedrock, which removes provider switching.
