"""Daily web tools using existing MCP transport and the shared OS URL opener."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.web_navigation import open_web_url
from agent_host.mcp_connections import McpConnectionSpec, open_mcp_connection


async def search_web(params: dict) -> dict:
    # Anonymous public search only: no account keys, paid fallback, local server
    # or separate Work. The model supplies the search intent to the remote tool.
    connection = McpConnectionSpec("pi-web-search", "Public web search", "http",
        url="https://mcp.exa.ai/mcp?tools=web_search_exa")
    async with asyncio.timeout(40):
        async with open_mcp_connection(connection) as client:
            result = await client.call_tool("web_search_exa", {
                "query": params["query"], "objective": params["objective"],
                "numResults": params.get("numResults", 5),
            })
    text = "\n".join(block.text for block in result.content if block.type == "text")
    return {"text": text[:24000], "truncated": len(text) > 24000,
        "is_error": result.is_error, "content_is_external": True,
        "visible_browser_opened": False}


def main():
    operation, params = sys.argv[1], json.loads(sys.argv[2])
    with redirect_stdout(sys.stderr):
        if operation == "open":
            result = open_web_url(params.get("url", ""))
        elif operation == "search":
            try:
                result = asyncio.run(search_web(params))
            except Exception:
                result = {"is_error": True,
                    "text": "Public web search is unavailable (connection, protocol or timeout). No results were obtained."}
        else:
            raise ValueError("unsupported_web_operation")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
