"""Content guardrails: find secrets and personal data in prompts before they leave (ADR-0010).

Each detector has a category, `secrets` or `pii`, and each team chooses an action per category:

- `off`: do not scan;
- `audit`: send the prompt unchanged, record what was found;
- `redact`: replace each finding with `[REDACTED:<detector>]` and send the rest;
- `block`: refuse the request, telling the client which kind of thing to remove.

The audit log records which detectors fired and how often, never the matched text. Detectors are
regular expressions, plus a checksum where one exists (card numbers): fast, predictable, and easy
to review. They will miss things a human would catch; they are a seatbelt, not a data-loss
prevention system.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .config import ContentPolicy, ContentRuleSpec


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Detector:
    name: str
    category: str
    pattern: re.Pattern[str]
    # Optional check on the matched value, to drop false positives (e.g. the Luhn checksum).
    valid: Callable[[str], bool] | None = None


# Order matters: more specific patterns run first, so `sk-ant-...` is reported as an Anthropic key
# and not also as a generic `sk-` key (a redacted span cannot match again).
BUILTIN_DETECTORS = (
    Detector("private_key", "secrets", re.compile(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )*PRIVATE KEY-----")),
    Detector("aws_access_key_id", "secrets", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # A secret key has no prefix, so only the value after a recognisable name is matched.
    Detector("aws_secret_access_key", "secrets", re.compile(
        r"(?i)aws_secret_access_key[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])")),
    Detector("github_token", "secrets", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    Detector("anthropic_api_key", "secrets", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    Detector("openai_api_key", "secrets", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    Detector("slack_token", "secrets", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    Detector("jwt", "secrets", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Detector("email", "pii", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
                                        r"\.[A-Za-z]{2,}\b")),
    Detector("credit_card", "pii", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), valid=_luhn),
    Detector("us_ssn", "pii", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
)

# Parts of a request that are not prose: identifiers, signatures, encoded media, settings.
_SKIP_KEYS = frozenset({"type", "id", "tool_use_id", "name", "signature", "data", "media_type",
                        "cache_control", "role", "model", "url"})
# Thinking blocks are signed by the model provider; changing one character makes the next request
# fail. They are the model's own output, sent back as is.
_SKIP_BLOCKS = frozenset({"thinking", "redacted_thinking"})


@dataclass
class Verdict:
    """What the guard found in one request and what it did about it."""

    findings: Counter[str] = field(default_factory=Counter)  # detector name -> count
    blocked_by: list[str] = field(default_factory=list)  # detectors whose category blocks
    redacted: bool = False

    def audit_fields(self) -> dict[str, Any]:
        if not self.findings:
            return {}
        action = "blocked" if self.blocked_by else "redacted" if self.redacted else "audited"
        return {"content_findings": dict(self.findings), "content_action": action}


class ContentGuard:
    def __init__(self, rules: Iterable[ContentRuleSpec] = ()) -> None:
        custom = tuple(Detector(r.name, r.category, re.compile(r.pattern)) for r in rules)
        clash = {d.name for d in BUILTIN_DETECTORS} & {d.name for d in custom}
        if clash:
            raise ValueError(f"content rules reuse built-in detector names: {sorted(clash)}")
        self.detectors = BUILTIN_DETECTORS + custom

    def _scan(self, text: str, policy: ContentPolicy, verdict: Verdict) -> str:
        for detector in self.detectors:
            action = getattr(policy, detector.category)
            if action == "off":
                continue

            def replace(match: re.Match[str], d: Detector = detector, action: str = action) -> str:
                group = 1 if match.re.groups else 0
                if d.valid and not d.valid(match.group(group)):
                    return match.group(0)
                verdict.findings[d.name] += 1
                if action == "block" and d.name not in verdict.blocked_by:
                    verdict.blocked_by.append(d.name)
                if action != "redact":
                    return match.group(0)
                verdict.redacted = True
                start, end = (s - match.start() for s in match.span(group))
                whole = match.group(0)
                return f"{whole[:start]}[REDACTED:{d.name}]{whole[end:]}"

            text = detector.pattern.sub(replace, text)
        return text

    def _walk(self, value: Any, policy: ContentPolicy, verdict: Verdict) -> Any:
        if isinstance(value, str):
            return self._scan(value, policy, verdict)
        if isinstance(value, list):
            return [self._walk(v, policy, verdict) for v in value]
        if isinstance(value, dict):
            if value.get("type") in _SKIP_BLOCKS:
                return value
            return {k: v if k in _SKIP_KEYS else self._walk(v, policy, verdict)
                    for k, v in value.items()}
        return value

    def screen(self, policy: ContentPolicy, body: dict[str, Any],
               fields: Iterable[str]) -> Verdict:
        """Scan (and, where the policy says so, redact) the given top-level fields of a request
        body in place. Raises nothing: the caller decides how to refuse a blocked request."""
        verdict = Verdict()
        if policy.secrets == "off" and policy.pii == "off":
            return verdict
        for name in fields:
            if name in body:
                body[name] = self._walk(body[name], policy, verdict)
        return verdict
