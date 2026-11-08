"""Per-person limits inside a team (ADR-0011): one person's runaway agent cannot use up the team."""

import asyncio
import io
import json

import httpx
from fastapi.testclient import TestClient
from test_mcp import FakeMcpServer, headers, rpc
from test_oidc import ISSUER, JWKS_URL, FakeIdp, token, verifier

from aigw import report
from aigw.auth import hash_key
from aigw.config import GatewayConfig
from aigw.main import create_app
from aigw.metering import Meter
from aigw.store import MemoryStore

KEY = "team-service-key"


def config(per_user):
    return GatewayConfig.model_validate({
        "oidc": {"issuer": ISSUER, "audience": "aigw", "jwks_url": JWKS_URL,
                 "user_claim": "preferred_username"},
        # $1 per token, so a short mock exchange costs a few dollars and budgets are easy to hit.
        "models": [{"name": "m", "provider": "mock",
                    "price": {"input_per_mtok": 1_000_000, "output_per_mtok": 1_000_000}}],
        "mcp_servers": [{"name": "jira", "url": "http://jira-mcp/mcp"}],
        "teams": [{"name": "platform", "oidc_groups": ["eng-platform"],
                   "key_sha256": [hash_key(KEY)], "allowed_tools": ["*"],
                   "requests_per_minute": 100, "budget": {"usd": 1000, "period": "day"},
                   "per_user": per_user}],
    })


def client(per_user):
    mcp_http = httpx.AsyncClient(transport=httpx.MockTransport(FakeMcpServer()))
    return TestClient(create_app(config(per_user), mcp_http=mcp_http, oidc=verifier(FakeIdp())))


def as_user(name):
    return {"Authorization": f"Bearer {token(preferred_username=name)}"}


def chat(gw, auth):
    return gw.post("/v1/chat/completions", headers=auth,
                   json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})


def test_each_person_has_their_own_rate_limit():
    gw = client({"requests_per_minute": 2})
    alice, bob = as_user("alice"), as_user("bob")
    assert [chat(gw, alice).status_code for _ in range(2)] == [200, 200]
    refused = chat(gw, alice)
    assert refused.status_code == 429
    assert refused.json()["error"]["message"] == "your personal rate limit is exceeded"
    assert "Retry-After" in refused.headers
    assert chat(gw, bob).status_code == 200  # a teammate is unaffected


def test_each_person_has_their_own_budget(caplog):
    gw = client({"budget": {"usd": 2, "period": "day"}})
    alice, bob = as_user("alice"), as_user("bob")
    assert chat(gw, alice).status_code == 200  # costs $3: 1 token in, 2 tokens out
    with caplog.at_level("INFO", logger="aigw.audit"):
        refused = chat(gw, alice)
    assert refused.status_code == 429
    assert refused.json()["error"]["message"] == "you have used your personal budget of $2 per day"
    assert refused.headers["Retry-After"] == "3600"  # tells Claude Code to stop retrying
    line = json.loads(caplog.records[-1].getMessage())
    assert (line["user"], line["reason"]) == ("alice", "user_budget_exceeded")
    assert chat(gw, bob).status_code == 200


def test_api_key_callers_only_have_team_limits():
    gw = client({"requests_per_minute": 1, "budget": {"usd": 1, "period": "day"}})
    service = {"Authorization": f"Bearer {KEY}"}
    assert [chat(gw, service).status_code for _ in range(3)] == [200, 200, 200]


def test_personal_rate_limit_applies_to_mcp_tool_calls():
    gw = client({"requests_per_minute": 1})
    alice = headers(token(preferred_username="alice"))
    call = rpc("tools/call", name="get_issue", arguments={"key": "OPS-1"})
    assert gw.post("/mcp/jira", json=call, headers=alice).status_code == 200
    refused = gw.post("/mcp/jira", json={**call, "id": 2}, headers=alice)
    assert refused.status_code == 429


def test_personal_spend_is_tracked_only_when_a_personal_budget_exists():
    async def scenario():
        store = MemoryStore()
        meter = Meter(store)
        with_budget = config({"budget": {"usd": 5, "period": "month"}}).teams[0]
        without = config({}).teams[0]
        await meter.charge(with_budget, 2.0, "alice")
        await meter.charge(without, 2.0, "bob")
        assert await meter.spent(with_budget, "alice") == 2.0
        assert await meter.spent(without, "bob") == 0.0  # no per-person key kept
        assert await meter.spent(with_budget) == 4.0  # both charges count for the team

    asyncio.run(scenario())


def test_report_shows_spend_per_person_and_per_served_model():
    lines = [json.dumps(e) for e in [
        {"event": "chat_completion", "team": "platform", "user": "alice", "model": "sonnet",
         "served_model": "haiku", "status": "ok", "cost_usd": 0.5},
        {"event": "chat_completion", "team": "platform", "user": "bob", "model": "sonnet",
         "status": "ok", "cost_usd": 2.0},
        {"event": "chat_completion", "team": "platform", "model": "sonnet", "status": "ok",
         "cost_usd": 1.0},
    ]]
    summary = report.summarize(lines)
    team = summary["teams"]["platform"]
    assert set(team["models"]) == {"haiku", "sonnet"}  # alice's request counts as haiku
    assert list(team["users"]) == ["bob", "alice"]  # most expensive first
    assert team["users"]["alice"] == {"requests": 1, "cost_usd": 0.5}
    out = io.StringIO()
    report.render(summary, out)
    assert "signed-in user" in out.getvalue()
