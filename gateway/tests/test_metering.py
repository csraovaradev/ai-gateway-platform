import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from aigw.auth import hash_key
from aigw.config import GatewayConfig, ModelSpec, TeamSpec
from aigw.main import create_app
from aigw.metering import Meter, cost_usd
from aigw.store import MemoryStore

KEY = "test-key"
# One dollar per token keeps the arithmetic readable: "hello gateway" is 2 prompt tokens and
# "echo: hello gateway" 3 completion tokens, so every mock request costs $5.
PRICE = {"input_per_mtok": 1_000_000, "output_per_mtok": 1_000_000}


def config(budget: dict | None = None) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "models": [{"name": "priced", "provider": "mock", "price": PRICE},
                   {"name": "free", "provider": "mock"}],
        "teams": [{"name": "t", "key_sha256": [hash_key(KEY)], "budget": budget}],
    })


def chat(client: TestClient, model: str = "priced", stream: bool = False):
    body = {"model": model, "stream": stream,
            "messages": [{"role": "user", "content": "hello gateway"}]}
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {KEY}"})


def test_cost_uses_model_price():
    haiku = ModelSpec(name="h", provider="bedrock", upstream_model="id",
                      price={"input_per_mtok": 1.0, "output_per_mtok": 5.0})
    assert cost_usd(haiku, 18, 35) == pytest.approx(0.000193)
    assert cost_usd(ModelSpec(name="m", provider="mock"), 18, 35) == 0.0


def test_audit_line_carries_cost(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        chat(TestClient(create_app(config())))
    assert json.loads(caplog.records[-1].getMessage())["cost_usd"] == 5.0


def test_budget_blocks_after_spend_is_reached(caplog):
    client = TestClient(create_app(config({"usd": 10, "period": "day"})))
    assert [chat(client).status_code for _ in range(2)] == [200, 200]
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = chat(client)
    assert resp.status_code == 429
    assert resp.json()["error"] == {"type": "insufficient_quota",
                                    "message": "team 't' has used its budget of $10 per day"}
    assert json.loads(caplog.records[-1].getMessage())["reason"] == "budget_exceeded"


def test_streaming_cannot_bypass_the_budget():
    client = TestClient(create_app(config({"usd": 5})))
    with client.stream("POST", "/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                       json={"model": "priced", "stream": True,
                             "messages": [{"role": "user", "content": "hello gateway"}]}) as r:
        r.read()
    assert chat(client, stream=True).status_code == 429


def test_unpriced_models_do_not_spend_budget():
    client = TestClient(create_app(config({"usd": 5})))
    assert [chat(client, "free").status_code for _ in range(3)] == [200, 200, 200]


@pytest.mark.parametrize(("now", "period", "key"), [
    (datetime(2026, 11, 30, 23, 0, tzinfo=UTC), "day", "spend:t:2026-11-30"),
    (datetime(2026, 11, 30, 23, 0, tzinfo=UTC), "month", "spend:t:2026-11"),
    (datetime(2026, 12, 31, 23, 0, tzinfo=UTC), "month", "spend:t:2026-12"),
])
def test_spend_key_per_period(now, period, key):
    team = TeamSpec(name="t", key_sha256=["x"], budget={"usd": 1, "period": period})
    found, ttl = Meter(MemoryStore(), clock=lambda: now)._period(team)
    assert found == key
    assert ttl == 2 * 3600  # one hour to the end of the period, plus one hour of grace


def test_daily_budget_resets_next_day():
    now = [datetime(2026, 11, 1, 12, tzinfo=UTC)]
    team = TeamSpec(name="t", key_sha256=["x"], budget={"usd": 1, "period": "day"})
    meter = Meter(MemoryStore(), clock=lambda: now[0])

    async def scenario():
        await meter.charge(team, 1.0)
        blocked = await meter.over_budget(team)
        now[0] = datetime(2026, 11, 2, 0, 1, tzinfo=UTC)
        return blocked, await meter.over_budget(team)

    assert asyncio.run(scenario()) == (True, False)
