#!/usr/bin/env python3
import argparse
import plistlib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the Amadeus wallpaper LaunchAgent plist.")
    parser.add_argument("--label", required=True)
    parser.add_argument("--program", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = {
        "Label": args.label,
        "ProgramArguments": [args.program],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "StandardOutPath": args.stdout,
        "StandardErrorPath": args.stderr,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)


if __name__ == "__main__":
    main()
