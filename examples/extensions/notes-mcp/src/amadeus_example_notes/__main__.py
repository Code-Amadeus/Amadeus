"""A small stdio MCP service for package-owned notes; no Host imports."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path


_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_MAX_LINE = 128 * 1024
_TOOLS = [
    {"name": "list_notes", "description": "List names of notes stored by this extension.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "read_note", "description": "Read one note from this extension's data directory.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"name": "save_note", "description": "Save a named note in this extension's data directory.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string", "maxLength": 10000}}, "required": ["name", "text"], "additionalProperties": False}},
]


def _tool(name, arguments, root: Path):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("Notes storage cannot be a link")
    root.mkdir(parents=True, exist_ok=True)
    if name == "list_notes":
        return json.dumps(sorted(item.stem for item in root.glob("*.txt")
                                 if item.is_file() and not item.is_symlink() and _NAME.fullmatch(item.stem)))
    note = arguments.get("name")
    if not isinstance(note, str) or not _NAME.fullmatch(note):
        raise ValueError("Note name must contain 1 to 64 letters, digits, underscores or hyphens")
    target = root / (note + ".txt")
    if target.is_symlink():
        raise ValueError("Notes cannot be symbolic links")
    if name == "read_note":
        if target.stat().st_size > 40000:
            raise ValueError("Note exceeds the size limit")
        return target.read_text(encoding="utf-8")
    if name != "save_note":
        raise ValueError("Unknown notes tool")
    text = arguments.get("text")
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("Note text must be at most 10000 characters")
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".note-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return "Note saved."


def serve(input_stream, output_stream, root: Path):
    while raw := input_stream.readline(_MAX_LINE + 1):
        if len(raw) > _MAX_LINE:
            return
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        method, params = request.get("method"), request.get("params") or {}
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            response["result"] = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "amadeus-example-notes", "version": "0.1.0"}}
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": _TOOLS}
        elif method == "tools/call":
            try:
                text = _tool(params.get("name"), params.get("arguments", {}), root)
                response["result"] = {"content": [{"type": "text", "text": text}], "isError": False}
            except (ValueError, OSError):
                response["result"] = {"content": [{"type": "text", "text": "The note operation failed; check its name and data directory."}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        output_stream.write(json.dumps(response, ensure_ascii=True) + "\n")
        output_stream.flush()


if __name__ == "__main__":
    directory = os.environ.get("AMADEUS_NOTES_DIR")
    if not directory:
        raise SystemExit("AMADEUS_NOTES_DIR is required")
    serve(sys.stdin, sys.stdout, Path(directory))
