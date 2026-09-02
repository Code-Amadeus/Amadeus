"""Print one Amadeus provider token for Codex command-backed authentication."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

from dotenv import dotenv_values

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.environment import secret_value

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def provider_token(*, env_key: str, env_file: Path) -> str:
    """Resolve one provider credential: process env first, else ``env_file``.

    Normalization is shared with config.environment (``secret_value``); the
    read chain stays here on purpose because ``env_file`` is an explicit
    Codex bridge input, not the project .env, so the project-level
    ``load_project_environment`` singleton does not apply. Edge policy: a
    process value that is empty/whitespace after normalization falls
    through to ``env_file``; a missing/empty result everywhere is an error.
    """
    key = str(env_key or "").strip()
    if not _ENV_KEY.fullmatch(key):
        raise ValueError("invalid provider credential environment variable name")
    inherited = secret_value(os.getenv(key))
    if inherited:
        return inherited
    values = dotenv_values(Path(env_file))
    token = secret_value(values.get(key))
    if not token:
        raise RuntimeError(f"provider credential {key} is not configured in Amadeus")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--env-key", required=True)
    args = parser.parse_args(argv)
    try:
        token = provider_token(env_key=args.env_key, env_file=args.env_file)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(token, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
