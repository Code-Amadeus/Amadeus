"""Synchronize one validated AUIP manifest into an HTML entry document.

The external ``auip.manifest.json`` remains the single authoring source and the
Host's pre-launch authority.  File-URL applications cannot reliably fetch JSON,
so the browser consumes a generated embedded copy.  This tool makes that copy
mechanical and checkable rather than a second hand-maintained contract.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.auip_contract import AuipProtocolError, parse_manifest  # noqa: E402


_EMBEDDED_MANIFEST = re.compile(
    r'(<script\s+id=["\']auip-manifest["\']\s+'
    r'type=["\']application/json["\']\s*>)[ \t]*\r?\n?'
    r'(.*?)'
    r'(\r?\n?[ \t]*</script>)',
    flags=re.DOTALL | re.IGNORECASE,
)


def sync_manifest(
    manifest_path: Path,
    entry_path: Path,
    *,
    check: bool = False,
) -> dict:
    try:
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        parse_manifest(source)
        html = entry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuipProtocolError("manifest_sync_read_failed", str(exc)) from exc
    match = _EMBEDDED_MANIFEST.search(html)
    if match is None:
        raise AuipProtocolError("embedded_manifest_missing", str(entry_path))
    try:
        embedded = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise AuipProtocolError("embedded_manifest_invalid", str(exc)) from exc
    if check:
        if embedded != source:
            raise AuipProtocolError("embedded_manifest_out_of_sync", str(entry_path))
        return source

    rendered = json.dumps(source, ensure_ascii=False, indent=2)
    generated = "\n".join(
        f"  {line}" if line else "" for line in rendered.splitlines()
    )
    updated = html[: match.start(2)] + generated + html[match.end(2) :]
    entry_path.write_text(updated, encoding="utf-8")
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("entry", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = sync_manifest(args.manifest, args.entry, check=args.check)
    except AuipProtocolError as exc:
        print(f"AUIP manifest sync failed: {exc}", file=sys.stderr)
        return 2
    action = "matches" if args.check else "synchronized"
    print(f"ok: embedded manifest {action} {manifest['app']['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
