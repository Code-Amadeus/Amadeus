"""Synthetic MCP wire peer; effects stay in the supplied test directory.

Use stdlib here so authority/lifecycle tests do not race the import time of an
unrelated Python SDK against the Host's intentionally bounded 3s discovery wait.
Pi's client/transport, tool discovery and permission pipeline are still real.
SDK-backed extension qualification remains in the separate Notes author journey.
"""
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["PI_MCP_FIXTURE_ROOT"])
root.mkdir(parents=True, exist_ok=True)
(root / "started.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
with (root / "pids.txt").open("a", encoding="utf-8") as output:
    output.write(f"{os.getpid()}\n")
inspect_name = os.environ.get("PI_MCP_INSPECT_NAME", "inspect_fixture")


def record(name):
    with (root / "calls.txt").open("a", encoding="utf-8") as output:
        output.write(name + "\n")


def dispatch(method, params):
    if method == "initialize":
        return {"protocolVersion": "2025-11-25", "serverInfo": {"name": "pi-fixture", "version": "1.0"},
                "capabilities": {"tools": {}, "resources": {}}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [
            {"name": inspect_name, "inputSchema": {"type": "object", "properties": {}},
             "annotations": {"readOnlyHint": True}},
            {"name": "write_note", "inputSchema": {"type": "object",
             "properties": {"text": {"type": "string"}}, "required": ["text"]},
             "annotations": {"readOnlyHint": False, "destructiveHint": False}},
        ]}
    if method == "tools/call":
        if params["name"] == inspect_name:
            record("inspect")
            text = json.dumps({"literal": os.environ.get("PI_MCP_LITERAL"),
                "unrelated": os.environ.get("AMADEUS_PI_TEST_HOST_SECRET"),
                "other_connection": os.environ.get("PI_MCP_OTHER_SECRET"),
                "host_registry": os.environ.get("AMADEUS_PI_MCP_SERVERS")})
        elif params["name"] == "write_note":
            record("write")
            (root / "note.txt").write_text(params["arguments"]["text"], encoding="utf-8")
            text = "Saved synthetic note"
        else:
            raise ValueError("Unknown fixture tool")
        return {"content": [{"type": "text", "text": text}], "isError": False}
    if method == "resources/list":
        return {"resources": [{"uri": "fixture://note", "name": "read_note", "mimeType": "text/plain"}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params["uri"] != "fixture://note":
            raise ValueError("Unknown fixture resource")
        record("resource")
        return {"contents": [{"uri": "fixture://note", "mimeType": "text/plain", "text": "Synthetic resource note"}]}
    raise NotImplementedError("Unknown fixture method")


if __name__ == "__main__":
    # MCP JSONL is UTF-8 regardless of the Windows interpreter locale.
    for line in sys.stdin.buffer:
        request = json.loads(line.decode("utf-8"))
        if "id" not in request:
            continue  # Initialized/cancelled notifications have no response.
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = dispatch(request["method"], request.get("params", {}))
        except NotImplementedError:
            response["error"] = {"code": -32601, "message": "Unknown fixture method"}
        except (KeyError, ValueError, TypeError):
            response["error"] = {"code": -32602, "message": "Invalid fixture request"}
        print(json.dumps(response, ensure_ascii=True), flush=True)
