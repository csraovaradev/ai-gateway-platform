import io
import json

from aigw.report import render, summarize

LINES = [
    'INFO:     Started server process [1]',  # server log noise is skipped
    json.dumps({"event": "chat_completion", "team": "data", "model": "bedrock-claude",
                "status": "ok", "prompt_tokens": 18, "completion_tokens": 35,
                "cost_usd": 0.000193}),
    json.dumps({"event": "chat_completion", "team": "data", "model": "bedrock-claude",
                "status": "ok", "prompt_tokens": 16, "completion_tokens": 13,
                "cost_usd": 0.000081}),
    json.dumps({"event": "chat_completion", "team": "data", "model": "bedrock-claude",
                "status": "denied", "reason": "budget_exceeded"}),
    json.dumps({"event": "chat_completion", "team": "platform", "model": "mock-echo",
                "status": "ok", "prompt_tokens": 2, "completion_tokens": 3, "cost_usd": 0.0}),
    json.dumps({"event": "mcp", "team": "data", "server": "jira", "method": "tools/call",
                "tool": "delete_issue", "status": "denied"}),
    json.dumps({"event": "mcp", "team": "data", "server": "jira", "method": "tools/list",
                "status": "ok"}),
    json.dumps({"event": "auth", "status": "denied", "reason": "invalid_api_key"}),
]


def test_summary_attributes_spend_per_team_and_model():
    summary = summarize(LINES)
    assert summary["total_cost_usd"] == 0.000274
    assert list(summary["teams"]) == ["data", "platform"]  # highest spend first
    data = summary["teams"]["data"]
    assert data["models"]["bedrock-claude"] == {
        "requests": 3, "denied": 1, "errors": 0, "prompt_tokens": 34,
        "completion_tokens": 48, "cost_usd": 0.000274}
    assert data["tools"] == {"jira/delete_issue": {"calls": 1, "denied": 1}}


def test_render_is_readable():
    out = io.StringIO()
    render(summarize(LINES), out)
    text = out.getvalue()
    assert text.startswith("Total spend: $0.000274")
    assert "jira/delete_issue" in text
