"""Synthetic stdio MCP peer; all effects stay in the supplied test directory."""
import asyncio
import json
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

root = Path(os.environ["PI_MCP_FIXTURE_ROOT"])
root.mkdir(parents=True, exist_ok=True)
(root / "started.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
with (root / "pids.txt").open("a", encoding="utf-8") as output:
    output.write(f"{os.getpid()}\n")
server = MCPServer("pi-fixture")


def record(name):
    with (root / "calls.txt").open("a", encoding="utf-8") as output:
        output.write(name + "\n")


@server.tool(name=os.environ.get("PI_MCP_INSPECT_NAME", "inspect_fixture"),
             annotations=ToolAnnotations(readOnlyHint=True))
def inspect_fixture() -> str:
    record("inspect")
    return json.dumps({"literal": os.environ.get("PI_MCP_LITERAL"),
        "unrelated": os.environ.get("AMADEUS_PI_TEST_HOST_SECRET"),
        "other_connection": os.environ.get("PI_MCP_OTHER_SECRET"),
        "host_registry": os.environ.get("AMADEUS_PI_MCP_SERVERS")})


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def write_note(text: str) -> str:
    record("write")
    (root / "note.txt").write_text(text, encoding="utf-8")
    return "Saved synthetic note"


@server.resource("fixture://note")
def read_note() -> str:
    record("resource")
    return "Synthetic resource note"


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
