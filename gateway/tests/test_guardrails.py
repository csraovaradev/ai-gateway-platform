"""Content guardrails (ADR-0010): secrets and personal data found, redacted, or blocked.

Fake secrets are assembled at runtime so that this file contains nothing a secret scanner (or
GitHub push protection) would mistake for a real credential.
"""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aigw import metrics
from aigw.auth import hash_key
from aigw.config import ContentPolicy, ContentRuleSpec, GatewayConfig
from aigw.guardrails import ContentGuard
from aigw.main import create_app
from aigw.providers.mock import MockProvider

KEY = "test-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}
AWS_KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"  # AWS's own documentation example
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
GITHUB_TOKEN = "gh" + "p_" + "a1B2" * 9
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "x" * 30
PRIVATE_KEY = ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "A" * 40
               + "\n-----END " + "RSA PRIVATE KEY-----")
TEST_CARD = "4111 1111 1111 1111"  # a standard test card number: passes the Luhn check
NOT_A_CARD = "4111 1111 1111 1112"  # one digit off: fails the Luhn check


def scan(text, secrets="redact", pii="redact", rules=()):
    body = {"messages": [{"role": "user", "content": text}]}
    verdict = ContentGuard(rules).screen(ContentPolicy(secrets=secrets, pii=pii), body,
                                         ("messages",))
    return body["messages"][0]["content"], verdict


@pytest.mark.parametrize(("secret", "detector"), [
    (AWS_KEY_ID, "aws_access_key_id"),
    (GITHUB_TOKEN, "github_token"),
    (ANTHROPIC_KEY, "anthropic_api_key"),
    (PRIVATE_KEY, "private_key"),
    ("xox" + "b-" + "1234567890-abcdefghij", "slack_token"),
    ("dev@example.com", "email"),
    (TEST_CARD, "credit_card"),
    ("123-45-6789", "us_ssn"),
])
def test_each_detector_finds_and_redacts(secret, detector):
    text, verdict = scan(f"please debug this: {secret} thanks")
    assert secret not in text and f"[REDACTED:{detector}]" in text
    assert text.startswith("please debug this: ") and text.endswith(" thanks")
    assert dict(verdict.findings) == {detector: 1}


def test_anthropic_key_is_not_also_reported_as_a_generic_key():
    _, verdict = scan(ANTHROPIC_KEY)
    assert dict(verdict.findings) == {"anthropic_api_key": 1}


def test_aws_secret_key_redacts_only_the_value():
    text, verdict = scan(f"aws_secret_access_key = {AWS_SECRET}")
    assert text == "aws_secret_access_key = [REDACTED:aws_secret_access_key]"
    assert verdict.findings["aws_secret_access_key"] == 1


def test_card_like_numbers_that_fail_the_checksum_are_left_alone():
    text, verdict = scan(f"order {NOT_A_CARD}")
    assert NOT_A_CARD in text and not verdict.findings


def test_ordinary_code_and_prose_are_untouched():
    prose = "def add(a, b):\n    return a + b  # see ADR-0010, version 1.2.3, port 8080"
    text, verdict = scan(prose)
    assert text == prose and not verdict.findings


def test_audit_action_records_without_changing_the_prompt():
    text, verdict = scan(f"key {AWS_KEY_ID}", secrets="audit")
    assert AWS_KEY_ID in text and verdict.findings["aws_access_key_id"] == 1
    assert verdict.audit_fields()["content_action"] == "audited"


def test_off_skips_scanning():
    text, verdict = scan(f"{AWS_KEY_ID} dev@example.com", secrets="off", pii="off")
    assert AWS_KEY_ID in text and not verdict.findings


def test_signed_thinking_ids_and_media_are_never_modified():
    body = {"messages": [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": f"the user pasted {AWS_KEY_ID}", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_" + AWS_KEY_ID, "name": "bash",
         "input": {"command": f"export KEY={AWS_KEY_ID}"}},
    ]}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_" + AWS_KEY_ID,
         "content": [{"type": "text", "text": f"KEY={AWS_KEY_ID}"}]},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": AWS_KEY_ID}},
    ]}]}
    ContentGuard().screen(ContentPolicy(), body, ("messages",))
    assistant, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert AWS_KEY_ID in assistant[0]["thinking"]  # signed: untouched
    assert assistant[1]["id"] == "toolu_" + AWS_KEY_ID  # identifiers: untouched
    assert AWS_KEY_ID not in assistant[1]["input"]["command"]  # tool input: redacted
    assert user[0]["tool_use_id"] == "toolu_" + AWS_KEY_ID
    assert AWS_KEY_ID not in user[0]["content"][0]["text"]  # tool output: redacted
    assert user[1]["source"]["data"] == AWS_KEY_ID  # encoded media: untouched


def test_custom_rules_from_the_policy():
    rule = ContentRuleSpec(name="internal_host", category="secrets",
                           pattern=r"\b[a-z0-9-]+\.corp\.example\.net\b")
    text, verdict = scan("connect to db-01.corp.example.net", rules=[rule])
    assert text == "connect to [REDACTED:internal_host]"
    with pytest.raises(ValidationError, match="invalid pattern"):
        ContentRuleSpec(name="bad", category="pii", pattern="(unclosed")
    with pytest.raises(ValueError, match="built-in"):
        ContentGuard([ContentRuleSpec(name="email", category="pii", pattern="x")])


# --- Through the gateway ---------------------------------------------------------------------


class Recording(MockProvider):
    """The mock model, keeping what reached it."""

    def __init__(self, spec):
        super().__init__(spec)
        self.seen = []

    async def complete(self, req, request_id):
        self.seen.append(req.model_dump_json())
        return await super().complete(req, request_id)

    async def messages(self, body, betas):
        self.seen.append(json.dumps(body))
        return await super().messages(body, betas)

    async def messages_stream(self, body, betas):
        self.seen.append(json.dumps(body))
        async for event in super().messages_stream(body, betas):
            yield event

    async def count_tokens(self, body, betas):
        self.seen.append(json.dumps(body))
        return await super().count_tokens(body, betas)


def gateway(content=None):
    cfg = GatewayConfig.model_validate({
        "models": [{"name": "m", "provider": "mock"}],
        "teams": [{"name": "t", "key_sha256": [hash_key(KEY)],
                   **({"content": content} if content else {})}]})
    provider = Recording(cfg.models[0])
    return TestClient(create_app(cfg, providers={"m": provider})), provider


def audit_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "aigw.audit"]


def test_secrets_are_redacted_before_reaching_the_model_by_default(caplog):
    client, provider = gateway()
    with caplog.at_level("INFO", logger="aigw.audit"):
        prompt = f"why does {AWS_KEY_ID} fail?"
        resp = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "m", "messages": [{"role": "user", "content": prompt}]})
    assert resp.status_code == 200
    assert AWS_KEY_ID not in provider.seen[0] and "[REDACTED:aws_access_key_id]" in provider.seen[0]
    line = audit_lines(caplog)[-1]
    assert line["content_findings"] == {"aws_access_key_id": 1}
    assert line["content_action"] == "redacted"
    assert AWS_KEY_ID not in caplog.text  # the audit log names the kind, never the value


def test_personal_data_is_audited_but_sent_by_default(caplog):
    client, provider = gateway()
    with caplog.at_level("INFO", logger="aigw.audit"):
        client.post("/v1/messages", headers=HEADERS, json={
            "model": "m", "max_tokens": 5,
            "messages": [{"role": "user", "content": "email dev@example.com"}]})
    assert "dev@example.com" in provider.seen[0]
    assert audit_lines(caplog)[-1]["content_findings"] == {"email": 1}


def test_blocking_team_gets_a_clear_refusal_and_nothing_is_sent(caplog):
    client, provider = gateway(content={"secrets": "block"})
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = client.post("/v1/messages", headers=HEADERS, json={
            "model": "m", "max_tokens": 5, "system": f"use key {GITHUB_TOKEN}",
            "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == (
        "request blocked by content policy: it contains github_token; remove it and retry")
    assert provider.seen == []
    line = audit_lines(caplog)[-1]
    assert (line["status"], line["reason"], line["content_action"]) == (
        "denied", "content_blocked", "blocked")
    assert GITHUB_TOKEN not in caplog.text


def test_streamed_messages_and_token_counting_are_screened_too():
    client, provider = gateway()
    message = {"model": "m", "max_tokens": 5,
               "messages": [{"role": "user", "content": f"key {ANTHROPIC_KEY}"}]}
    assert client.post("/v1/messages", headers=HEADERS,
                       json={**message, "stream": True}).status_code == 200
    assert client.post("/v1/messages/count_tokens", headers=HEADERS,
                       json=message).status_code == 200
    assert len(provider.seen) == 2 and all(ANTHROPIC_KEY not in s for s in provider.seen)


def test_findings_are_counted_in_metrics():
    def count():
        return metrics.REGISTRY.get_sample_value("aigw_content_findings_total", {
            "team": "t", "detector": "private_key", "action": "redacted"}) or 0

    before = count()
    client, _ = gateway()
    client.post("/v1/chat/completions", headers=HEADERS, json={
        "model": "m", "messages": [{"role": "user", "content": PRIVATE_KEY}]})
    assert count() == before + 1


def test_policy_values_are_validated():
    with pytest.raises(ValidationError):
        ContentPolicy(secrets="encrypt")
