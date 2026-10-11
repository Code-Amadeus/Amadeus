"""Static extension package validation CLI. JSON only; safe under Python -I."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.extensions.package import inspect_wheel, validate_config, validate_installed, validate_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect-wheel", "validate-config", "validate-manifest", "validate-installed"])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--manifest")
    parser.add_argument("--config")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-wheel":
            result = inspect_wheel(Path(args.path or ""))
        else:
            if args.manifest:
                body = {"manifest": json.loads(Path(args.manifest).read_text(encoding="utf-8")),
                        "config": json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {},
                        "allow_incomplete": args.allow_incomplete}
            else:
                raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("validation input exceeds 1 MiB")
                body = json.loads(raw.decode("utf-8"))
            manifest = validate_manifest(body["manifest"])
            if args.command == "validate-installed":
                validate_installed(manifest, Path(args.path or ""))
                result = {}
            elif args.command == "validate-manifest":
                result = {"manifest": manifest}
            else:
                resolved, missing = validate_config(manifest, body.get("config", {}), allow_incomplete=bool(body.get("allow_incomplete")))
                result = {"config": {key: value for key, value in resolved.items() if not manifest.get("config", {}).get(key, {}).get("secret")}, "missing": missing}
        print(json.dumps({"ok": True, **result}, ensure_ascii=True, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, EOFError, UnicodeError, zipfile.BadZipFile) as exc:
        # Config validators never include values; malformed JSON may carry secret input.
        error = "invalid JSON input" if isinstance(exc, json.JSONDecodeError) else "invalid UTF-8 input" if isinstance(exc, UnicodeError) else str(exc) if isinstance(exc, ValueError) else "package validation failed"
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
