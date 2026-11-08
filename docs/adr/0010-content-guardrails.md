# ADR-0010: Content guardrails for secrets and personal data

- **Status:** Accepted
- **Date:** 2026-11-08
- **Follows:** ADR-0006, ADR-0007, ADR-0009

## Context

Everything so far governs access: who may call which model and tool, at what cost, with what record.
None of it looks at what is in a prompt. Developers paste stack traces, config files, and terminal
output into coding agents, and those often contain cloud keys, API tokens, private keys, or customer
email addresses. Once a prompt reaches a model provider, the secret has left the company.

The threat model listed this as an accepted risk ("no content inspection"), and the companion article
named it as the main thing a front door cannot stop.

## Decision

1. **Detectors find secrets and personal data in prompts** before they are sent upstream.
   - **Secrets:** private keys, AWS access key IDs, AWS secret keys next to their variable name,
     GitHub, Anthropic, OpenAI, and Slack tokens, JWTs.
   - **Personal data:** email addresses, card numbers (only if they pass the Luhn checksum), US
     social security numbers.
   - Policies can add their own (`content_rules`: a name, a category, a regular expression), for
     example internal hostnames or project code names.
2. **Each team chooses an action per category** (`content: {secrets, pii}`):
   - `off`: no scanning.
   - `audit`: send unchanged, record the findings.
   - `redact`: replace each finding with `[REDACTED:<detector>]` and send the rest.
   - `block`: refuse with 400, telling the client which kind of thing to remove.

   Defaults: **redact secrets, audit personal data.** Blocking everything pushes people to work
   around the gateway; only auditing lets secrets reach the provider; redaction keeps work going and
   keeps the secret inside. Personal data defaults to audit because email addresses in code and
   logs are common and often harmless; teams handling customer data can switch to `redact` or `block`.
3. **What is scanned:** the system prompt and every message, including tool inputs and tool
   results, on `/v1/chat/completions`, `/v1/messages` (streamed or not), and
   `/v1/messages/count_tokens`, since token counting also sends the prompt upstream.
4. **What is never modified:** identifiers (`id`, `tool_use_id`), encoded media (`data`), settings,
   and **signed thinking blocks**. Changing one character of a thinking block makes the provider
   reject the next request, and they are the model's own output sent back as is.
5. **Never record the value.** Audit lines carry `content_findings` (detector name and count) and
   `content_action`; `aigw_content_findings_total` counts by team, detector, and action. Detector names
   come from code and policy, so metric labels stay bounded (ADR-0004).
6. **Redaction is deterministic,** so the same conversation is redacted identically on every turn
   and the provider's prompt cache keeps working.

## Alternatives considered

- **A model-based classifier** (an LLM or NER model judging each prompt): catches more, but adds
  latency and cost to every request, is hard to test, and is itself a place prompts are sent.
  The detector list is a seam where one could be added later.
- **Scanning responses** on the way back: not done. The risk this addresses is data leaving the
  company; responses come from the provider the company already chose.
- **Scanning MCP tool arguments:** not done. Those calls go to internal servers behind the gateway,
  and the tool allowlist (ADR-0002) is the control there.

## Consequences

- Common secrets no longer reach model providers, and the audit log shows how often they would have.
- Regular expressions miss things a person would catch (a secret in an unusual format, personal data
  without a fixed shape) and occasionally flag something harmless. This is a seatbelt, not a
  data-loss prevention system.
- Redaction changes what the agent can do: asked to write a pasted key into a file, it writes the
  placeholder, because that is all it received. That is the intent, but users should know why.
- Verified live with Claude Code on Bedrock: a prompt containing an AWS key was redacted on all three
  turns of a write-then-read task, the file on disk contained only the placeholder, tool use worked
  normally, and the key appeared nowhere in the gateway's logs.
