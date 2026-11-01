"""Demo MCP server ("jira") with three tools, served over Streamable HTTP on :9000/mcp.

    pip install "mcp>=2"
    python examples/mcp-demo/server.py
"""

from mcp.server.mcpserver import MCPServer

ISSUES = {"OPS-1": "Gateway returns 502 when Vault is sealed", "OPS-2": "Add MCP audit dashboard"}

server = MCPServer("jira-demo")


@server.tool()
def search(query: str) -> list[str]:
    """Find issue keys whose title contains the query."""
    return [key for key, title in ISSUES.items() if query.lower() in title.lower()]


@server.tool()
def get_issue(key: str) -> str:
    """Return an issue's title."""
    return ISSUES.get(key, "not found")


@server.tool()
def delete_issue(key: str) -> str:
    """Delete an issue. Destructive: only some teams should be able to call this."""
    return f"deleted {key}" if ISSUES.pop(key, None) else "not found"


if __name__ == "__main__":
    server.run("streamable-http", host="127.0.0.1", port=9000)
