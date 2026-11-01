"""Call the demo MCP server through the gateway with the official MCP SDK.

    python examples/mcp-demo/client.py dev-data-key legacy
    python examples/mcp-demo/client.py dev-platform-key 2026-07-28
"""

import os
import sys

import anyio
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

GATEWAY = os.environ.get("AIGW_MCP_URL", "http://127.0.0.1:8080/mcp/jira")


async def main(key: str, mode: str) -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"})
    async with Client(streamable_http_client(GATEWAY, http_client=http), mode=mode) as client:
        tools = await client.list_tools()
        print("visible tools:", [t.name for t in tools.tools])
        for name, args in [("get_issue", {"key": "OPS-1"}), ("delete_issue", {"key": "OPS-2"})]:
            try:
                result = await client.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                print(f"{name}: {'error' if result.is_error else 'ok'}: {text}")
            except Exception as exc:  # the SDK raises on JSON-RPC errors
                print(f"{name}: refused: {exc}")


if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "auto")
